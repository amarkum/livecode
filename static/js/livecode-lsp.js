(function () {
    "use strict";

    var REQUEST_TIMEOUT_MS = 15000;
    var MAX_RECONNECT = 5;
    var PY_EXT = /\.py[iw]?$/i;

    var _providersInstalled = false;
    var _openCodeEditorPatched = false;
    var activeClient = null;
    var _preloadedUris = new Set();
    var _preloadSeq = 0;

    function mUri(path) {
      return window.monaco.Uri.file(path);
    }
    function docUri(path) {
      return mUri(path).toString();
    }
    function modelForLspUri(uri) {
      try {
        return window.monaco.editor.getModel(window.monaco.Uri.parse(uri));
      } catch (e) {
        return null;
      }
    }
    function toMonacoPos(p) {
      return { lineNumber: (p.line || 0) + 1, column: (p.character || 0) + 1 };
    }
    function toLspPos(lineNumber, column) {
      return { line: lineNumber - 1, character: column - 1 };
    }
    function toMonacoRange(r) {
      return new window.monaco.Range(
        r.start.line + 1,
        r.start.character + 1,
        r.end.line + 1,
        r.end.character + 1
      );
    }
    function lspRangeFromModel(model, range) {
      return {
        start: toLspPos(range.startLineNumber, range.startColumn),
        end: toLspPos(range.endLineNumber, range.endColumn),
      };
    }

    function ensureStatusEl() {
      var existing = document.getElementById("livecode-lsp-status");
      if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
      return null;
    }

    function setStatus(state, text, detail) {
      try {
        window.WBLspStatus = { state: state || "idle", text: text || "Python LSP: idle", detail: detail || "" };
        if (typeof window.filesUpdateOpenTabsList === "function") window.filesUpdateOpenTabsList();
        var el = ensureStatusEl();
        if (!el) return;
        el.className = "livecode-lsp-status is-" + (state || "idle");
        el.textContent = text || "Python LSP: idle";
        el.title = detail || "";
      } catch (_) {}
    }

    function LspClient(projectPath) {
      this.projectPath = projectPath;
      this.rootUri = mUri(projectPath).toString();
      this.ws = null;
      this.ready = false;
      this.disposed = false;
      this.reconnects = 0;
      this.nextId = 1;
      this.pending = Object.create(null);
      this.versions = Object.create(null);
      this.open = Object.create(null);
      this.queue = [];
      this.modelListeners = Object.create(null);
      this._connect();
    }

    LspClient.prototype._connect = function () {
      if (this.disposed) return;
      var self = this;
      var base = (location.origin || location.protocol + "//" + location.host).replace(/^http/, "ws");
      var wsUrl = base + "/livecode/lsp/python?project=" + encodeURIComponent(this.projectPath);
      var ws;
      setStatus("connecting", "Python LSP: connecting");
      try {
        ws = new WebSocket(wsUrl);
      } catch (e) {
        setStatus("unavailable", "Python LSP: unavailable", e && e.message ? e.message : "WebSocket failed");
        return;
      }
      this.ws = ws;
      ws.onopen = function () {
        self.reconnects = 0;
        self.everOpened = true;
        setStatus("connecting", "Python LSP: starting");
        self._initialize();
      };
      ws.onmessage = function (ev) {
        var msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        self._dispatch(msg);
      };
      ws.onclose = function (ev) {
        self.ready = false;
        if (self.disposed) return;
        var reason = ev && ev.reason ? ev.reason : "Language server connection closed";
        // A socket that never opened means the server has no language-server bridge
        // (e.g. flask-sock is not installed): one retry, not a reconnect loop.
        var limit = self.everOpened ? MAX_RECONNECT : 1;
        if (self.reconnects++ < limit) {
          setStatus("reconnecting", "Python LSP: reconnecting", reason);
          setTimeout(function () {
            self._connect();
          }, Math.min(500 * self.reconnects, 4000));
        } else {
          setStatus("unavailable", "Python LSP: unavailable", reason);
        }
      };
      ws.onerror = function () {
        setStatus("unavailable", "Python LSP: unavailable", "WebSocket error");
        try {
          ws.close();
        } catch (e) {}
      };
    };

    LspClient.prototype._send = function (obj) {
      if (!this.ws || this.ws.readyState !== 1) return;
      this.ws.send(JSON.stringify(obj));
    };

    LspClient.prototype.request = function (method, params) {
      var self = this;
      var id = this.nextId++;
      var payload = { jsonrpc: "2.0", id: id, method: method, params: params || {} };
      return new Promise(function (resolve, reject) {
        var timer = setTimeout(function () {
          delete self.pending[id];
          reject(new Error("LSP timeout: " + method));
        }, REQUEST_TIMEOUT_MS);
        self.pending[id] = {
          resolve: function (v) {
            clearTimeout(timer);
            resolve(v);
          },
          reject: function (e) {
            clearTimeout(timer);
            reject(e);
          },
        };
        if (self.ready || method === "initialize") self._send(payload);
        else self.queue.push(payload);
      });
    };

    LspClient.prototype.notify = function (method, params) {
      var payload = { jsonrpc: "2.0", method: method, params: params || {} };
      if (this.ready || method === "initialized") this._send(payload);
      else this.queue.push(payload);
    };

    LspClient.prototype._initialize = function () {
      var self = this;
      this.request("initialize", {
        processId: null,
        clientInfo: { name: "LiveCode", version: "1" },
        rootUri: this.rootUri,
        workspaceFolders: [{ uri: this.rootUri, name: this.projectPath.split("/").filter(Boolean).pop() || "project" }],
        capabilities: {
          textDocument: {
            synchronization: { dynamicRegistration: false, didSave: false },
            hover: { contentFormat: ["markdown", "plaintext"] },
            definition: { linkSupport: false },
            references: {},
            documentSymbol: { hierarchicalDocumentSymbolSupport: true },
            completion: {
              completionItem: {
                snippetSupport: true,
                documentationFormat: ["markdown", "plaintext"],
              },
              contextSupport: true,
            },
            signatureHelp: { signatureInformation: { documentationFormat: ["markdown", "plaintext"] } },
            rename: { prepareSupport: true },
            publishDiagnostics: { relatedInformation: true },
          },
          workspace: { workspaceFolders: true, configuration: true },
        },
      })
        .then(function () {
          self.ready = true;
          setStatus("ready", "Python LSP: ready");
          self.notify("initialized", {});
          var q = self.queue;
          self.queue = [];
          q.forEach(function (m) {
            self._send(m);
          });
        })
        .catch(function (e) {
          setStatus("unavailable", "Python LSP: unavailable", e && e.message ? e.message : "Initialize failed");
        });
    };

    LspClient.prototype._dispatch = function (msg) {
      if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
        var p = this.pending[msg.id];
        if (!p) return;
        delete this.pending[msg.id];
        if (msg.error) p.reject(new Error(msg.error.message || "LSP error"));
        else p.resolve(msg.result);
        return;
      }
      if (msg.method === "textDocument/publishDiagnostics") {
        this._applyDiagnostics(msg.params || {});
        return;
      }
      if (msg.id !== undefined && msg.method) {
        var res = null;
        if (msg.method === "workspace/configuration") {
          res = (msg.params.items || []).map(function () {
            return {};
          });
        } else if (msg.method === "client/registerCapability" || msg.method === "window/workDoneProgress/create") {
          res = null;
        }
        this._send({ jsonrpc: "2.0", id: msg.id, result: res });
      }
    };

    LspClient.prototype._applyDiagnostics = function (params) {
      var model = modelForLspUri(params.uri);
      if (!model) return;
      var sev = window.monaco.MarkerSeverity;
      var markers = (params.diagnostics || []).map(function (d) {
        var r = toMonacoRange(d.range);
        var s = sev.Error;
        if (d.severity === 2) s = sev.Warning;
        else if (d.severity === 3) s = sev.Info;
        else if (d.severity === 4) s = sev.Hint;
        return {
          severity: s,
          message: d.message || "",
          source: d.source || "pylsp",
          code: d.code != null ? String(d.code) : undefined,
          startLineNumber: r.startLineNumber,
          startColumn: r.startColumn,
          endLineNumber: r.endLineNumber,
          endColumn: r.endColumn,
        };
      });
      try {
        window.monaco.editor.setModelMarkers(model, "pylsp", markers);
      } catch (e) {}
    };

    LspClient.prototype.openDoc = function (path, model) {
      if (this.disposed || !model) return;
      var uri = docUri(path);
      if (this.open[uri]) return;
      this.open[uri] = true;
      this.versions[uri] = 1;
      this.notify("textDocument/didOpen", {
        textDocument: { uri: uri, languageId: "python", version: 1, text: model.getValue() },
      });
      var self = this;
      var d = model.onDidChangeContent(function () {
        if (self.disposed || !self.open[uri]) return;
        self.versions[uri] = (self.versions[uri] || 1) + 1;
        self.notify("textDocument/didChange", {
          textDocument: { uri: uri, version: self.versions[uri] },
          contentChanges: [{ text: model.getValue() }],
        });
      });
      var gone = model.onWillDispose(function () {
        self.closeDoc(path);
      });
      this.modelListeners[uri] = {
        dispose: function () {
          d.dispose();
          gone.dispose();
        },
      };
    };

    LspClient.prototype.closeDoc = function (path) {
      var uri = docUri(path);
      if (!this.open[uri]) return;
      delete this.open[uri];
      if (this.modelListeners[uri]) {
        try {
          this.modelListeners[uri].dispose();
        } catch (e) {}
        delete this.modelListeners[uri];
      }
      this.notify("textDocument/didClose", { textDocument: { uri: uri } });
    };

    LspClient.prototype.dispose = function () {
      this.disposed = true;
      this.ready = false;
      var self = this;
      Object.keys(this.modelListeners).forEach(function (uri) {
        try {
          self.modelListeners[uri].dispose();
        } catch (e) {}
      });
      this.modelListeners = Object.create(null);
      try {
        if (this.ws && this.ws.readyState === 1) this.notify("shutdown", {});
      } catch (e) {}
      try {
        if (this.ws) this.ws.close();
      } catch (e) {}
      try {
        window.monaco.editor.getModels().forEach(function (m) {
          window.monaco.editor.setModelMarkers(m, "pylsp", []);
        });
      } catch (e) {}
    };

    function client() {
      return activeClient && activeClient.ready ? activeClient : null;
    }

    function lspDocParams(model, position) {
      return {
        textDocument: { uri: model.uri.toString() },
        position: toLspPos(position.lineNumber, position.column),
      };
    }

    function mdString(v) {
      if (!v) return null;
      if (typeof v === "string") return { value: v };
      if (v.kind) return { value: v.value || "" };
      if (v.language) return { value: "```" + v.language + "\n" + v.value + "\n```" };
      return { value: String(v.value || "") };
    }

    function locationsToLinks(result) {
      if (!result) return [];
      var arr = Array.isArray(result) ? result : [result];
      return arr
        .map(function (loc) {
          var uri = loc.uri || loc.targetUri;
          var range = loc.range || loc.targetSelectionRange || loc.targetRange;
          if (!uri || !range) return null;
          return { uri: window.monaco.Uri.parse(uri), range: toMonacoRange(range) };
        })
        .filter(Boolean);
    }

    function installProviders() {
      if (_providersInstalled || !window.monaco || !window.monaco.languages) return;
      _providersInstalled = true;
      var L = window.monaco.languages;
      var SEL = "python";

      L.registerHoverProvider(SEL, {
        provideHover: function (model, position) {
          var c = client();
          if (!c) return null;
          return c.request("textDocument/hover", lspDocParams(model, position)).then(function (r) {
            if (!r || !r.contents) return null;
            var parts = Array.isArray(r.contents) ? r.contents : [r.contents];
            var contents = parts.map(mdString).filter(Boolean);
            return { range: r.range ? toMonacoRange(r.range) : undefined, contents: contents };
          }, function () {
            return null;
          });
        },
      });

      L.registerDefinitionProvider(SEL, {
        provideDefinition: function (model, position) {
          var c = client();
          if (!c) return null;
          return c
            .request("textDocument/definition", lspDocParams(model, position))
            .then(locationsToLinks, function () {
              return [];
            });
        },
      });

      L.registerReferenceProvider(SEL, {
        provideReferences: function (model, position, context) {
          var c = client();
          if (!c) return null;
          var params = lspDocParams(model, position);
          params.context = { includeDeclaration: !!(context && context.includeDeclaration) };
          return c.request("textDocument/references", params).then(locationsToLinks, function () {
            return [];
          });
        },
      });

      L.registerDocumentSymbolProvider(SEL, {
        provideDocumentSymbols: function (model) {
          var c = client();
          if (!c) return null;
          return c
            .request("textDocument/documentSymbol", { textDocument: { uri: model.uri.toString() } })
            .then(function (r) {
              if (!r) return [];
              function flatten(sym, container) {
                var kind = sym.kind || 13;
                var range = sym.range || (sym.location && sym.location.range);
                var sel = sym.selectionRange || range;
                if (!range) return [];
                var node = {
                  name: sym.name,
                  detail: sym.detail || "",
                  kind: kind - 1,
                  tags: [],
                  range: toMonacoRange(range),
                  selectionRange: toMonacoRange(sel),
                  containerName: container,
                };
                var out = [node];
                (sym.children || []).forEach(function (ch) {
                  out = out.concat(flatten(ch, sym.name));
                });
                return out;
              }
              var res = [];
              (Array.isArray(r) ? r : [r]).forEach(function (s) {
                res = res.concat(flatten(s, undefined));
              });
              return res;
            }, function () {
              return [];
            });
        },
      });

      L.registerCompletionItemProvider(SEL, {
        triggerCharacters: [".", "(", "[", '"', "'", " "],
        provideCompletionItems: function (model, position) {
          var c = client();
          if (!c) return { suggestions: [] };
          var word = model.getWordUntilPosition(position);
          var defaultRange = new window.monaco.Range(
            position.lineNumber,
            word.startColumn,
            position.lineNumber,
            word.endColumn
          );
          return c
            .request("textDocument/completion", lspDocParams(model, position))
            .then(function (r) {
              var items = !r ? [] : Array.isArray(r) ? r : r.items || [];
              var Kind = window.monaco.languages.CompletionItemKind;
              return {
                incomplete: !!(r && r.isIncomplete),
                suggestions: items.map(function (it) {
                  var edit = it.textEdit;
                  var insert = (edit && (edit.newText != null ? edit.newText : edit.insert)) || it.insertText || it.label;
                  var range = defaultRange;
                  if (edit && edit.range) range = toMonacoRange(edit.range);
                  return {
                    label: it.label,
                    kind: Kind[kindName(it.kind)] != null ? Kind[kindName(it.kind)] : Kind.Text,
                    detail: it.detail || "",
                    documentation: it.documentation ? mdString(it.documentation) : undefined,
                    insertText: insert,
                    insertTextRules:
                      it.insertTextFormat === 2
                        ? window.monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
                        : undefined,
                    range: range,
                    sortText: it.sortText || undefined,
                    filterText: it.filterText || undefined,
                    commitCharacters: it.commitCharacters || undefined,
                  };
                }),
              };
            }, function () {
              return { suggestions: [] };
            });
        },
      });

      L.registerSignatureHelpProvider(SEL, {
        signatureHelpTriggerCharacters: ["(", ","],
        signatureHelpRetriggerCharacters: [")"],
        provideSignatureHelp: function (model, position) {
          var c = client();
          if (!c) return null;
          return c.request("textDocument/signatureHelp", lspDocParams(model, position)).then(function (r) {
            if (!r || !r.signatures || !r.signatures.length) return null;
            return {
              value: {
                signatures: r.signatures.map(function (s) {
                  return {
                    label: s.label,
                    documentation: s.documentation ? mdString(s.documentation) : undefined,
                    parameters: (s.parameters || []).map(function (p) {
                      return {
                        label: p.label,
                        documentation: p.documentation ? mdString(p.documentation) : undefined,
                      };
                    }),
                  };
                }),
                activeSignature: r.activeSignature || 0,
                activeParameter: r.activeParameter || 0,
              },
              dispose: function () {},
            };
          }, function () {
            return null;
          });
        },
      });

      L.registerRenameProvider(SEL, {
        provideRenameEdits: function (model, position, newName) {
          var c = client();
          if (!c) return null;
          var params = lspDocParams(model, position);
          params.newName = newName;
          return c.request("textDocument/rename", params).then(function (we) {
            return workspaceEditToMonaco(we);
          }, function () {
            return { edits: [] };
          });
        },
        resolveRenameLocation: function (model, position) {
          var c = client();
          if (!c) return null;
          return c
            .request("textDocument/prepareRename", lspDocParams(model, position))
            .then(function (r) {
              if (!r) return null;
              var range = r.range || r;
              var text = model.getValueInRange(toMonacoRange(range));
              return { range: toMonacoRange(range), text: text };
            }, function () {
              return null;
            });
        },
      });
    }

    function kindName(k) {
      var names = [
        "Text", "Method", "Function", "Constructor", "Field", "Variable", "Class",
        "Interface", "Module", "Property", "Unit", "Value", "Enum", "Keyword",
        "Snippet", "Color", "File", "Reference", "Folder", "EnumMember", "Constant",
        "Struct", "Event", "Operator", "TypeParameter",
      ];
      return names[(k || 1) - 1] || "Text";
    }

    function workspaceEditToMonaco(we) {
      var edits = [];
      if (!we) return { edits: edits };
      var changes = we.changes;
      if (we.documentChanges) {
        changes = {};
        we.documentChanges.forEach(function (dc) {
          if (dc.textDocument && dc.edits) changes[dc.textDocument.uri] = dc.edits;
        });
      }
      Object.keys(changes || {}).forEach(function (uri) {
        changes[uri].forEach(function (e) {
          edits.push({
            resource: window.monaco.Uri.parse(uri),
            versionId: undefined,
            textEdit: { range: toMonacoRange(e.range), text: e.newText },
          });
        });
      });
      return { edits: edits };
    }

    function patchOpenCodeEditor(editor) {
      if (_openCodeEditorPatched || !editor) return;
      var svc = editor._codeEditorService || (editor.getContribution && null);
      if (!svc || typeof svc.openCodeEditor !== "function") return;
      _openCodeEditorPatched = true;
      var original = svc.openCodeEditor.bind(svc);

      function openViaTabs(input) {
        try {
          var res = input.resource || {};
          var path = res.fsPath || res.path || "";
          if (path && typeof window.openFileInEditorFromPath === "function") {
            var sel = input.options && input.options.selection;
            window.openFileInEditorFromPath(
              path,
              sel ? { lineNumber: sel.startLineNumber, column: sel.startColumn } : {}
            );
            if (sel) {
              setTimeout(function () {
                try {
                  window.ideEditor.setSelection(sel);
                  window.ideEditor.revealRangeInCenter(sel);
                } catch (e) {}
              }, 140);
            }
          }
        } catch (e) {}
      }

      svc.openCodeEditor = function (input, sourceEditor, sideBySide) {
        var pending;
        try {
          pending = Promise.resolve(original(input, sourceEditor, sideBySide));
        } catch (e) {
          openViaTabs(input);
          return Promise.resolve(sourceEditor);
        }
        return pending.then(
          function (result) {
            if (result) return result;
            openViaTabs(input);
            return sourceEditor;
          },
          function () {
            openViaTabs(input);
            return sourceEditor;
          }
        );
      };
    }

    var _navAttached = false;
    var _refState = { key: "", list: [], idx: 0 };

    function navToast(msg) {
      if (typeof window._livecodeShowIdeToast === "function") window._livecodeShowIdeToast(msg);
    }

    function revealLocation(loc) {
      var monaco = window.monaco;
      var r = loc.range;
      var sel = new monaco.Selection(r.startLineNumber, r.startColumn, r.startLineNumber, r.startColumn);
      var cur = window.ideEditor && window.ideEditor.getModel();
      if (cur && cur.uri.toString() === loc.uri.toString()) {
        window.ideEditor.setSelection(sel);
        window.ideEditor.revealRangeInCenterIfOutsideViewport(r);
        window.ideEditor.focus();
        return;
      }
      var path = loc.uri.fsPath || loc.uri.path;
      if (path && typeof window.openFileInEditorFromPath === "function") {
        window.openFileInEditorFromPath(path, { lineNumber: r.startLineNumber, column: r.startColumn });
        setTimeout(function () {
          try {
            window.ideEditor.setSelection(sel);
            window.ideEditor.revealRangeInCenter(r);
            window.ideEditor.focus();
          } catch (e) {}
        }, 160);
      }
    }

    function runDefinition(position) {
      var c = client();
      var model = window.ideEditor && window.ideEditor.getModel();
      if (!c || !model) return;
      var pos = position || window.ideEditor.getPosition();
      c.request("textDocument/definition", {
        textDocument: { uri: model.uri.toString() },
        position: toLspPos(pos.lineNumber, pos.column),
      }).then(
        function (res) {
          var links = locationsToLinks(res);
          if (!links.length) {
            navToast("No definition found");
            return;
          }
          revealLocation(links[0]);
        },
        function () {
          navToast("Definition lookup failed");
        }
      );
    }

    function runReferences(position) {
      var c = client();
      var model = window.ideEditor && window.ideEditor.getModel();
      if (!c || !model) return;
      var pos = position || window.ideEditor.getPosition();
      var word = model.getWordAtPosition(pos);
      var key = model.uri.toString() + ":" + (word ? word.word : "") + ":" + pos.lineNumber;
      if (key === _refState.key && _refState.list.length) {
        _refState.idx = (_refState.idx + 1) % _refState.list.length;
        revealLocation(_refState.list[_refState.idx]);
        navToast("Reference " + (_refState.idx + 1) + " / " + _refState.list.length);
        return;
      }
      c.request("textDocument/references", {
        textDocument: { uri: model.uri.toString() },
        position: toLspPos(pos.lineNumber, pos.column),
        context: { includeDeclaration: true },
      }).then(
        function (res) {
          var links = locationsToLinks(res);
          if (!links.length) {
            navToast("No references found");
            return;
          }
          _refState = { key: key, list: links, idx: 0 };
          revealLocation(links[0]);
          navToast(
            links.length === 1 ? "1 reference" : "Reference 1 / " + links.length + "  (Shift+F12 to cycle)"
          );
        },
        function () {
          navToast("Reference lookup failed");
        }
      );
    }

    function attachNavigation(editor) {
      if (_navAttached || !editor || !window.monaco) return;
      _navAttached = true;
      var monaco = window.monaco;
      var KM = monaco.KeyMod;
      var KC = monaco.KeyCode;

      editor.addAction({
        id: "livecode.gotoDefinition",
        label: "Go to Definition",
        keybindings: [KC.F12],
        contextMenuGroupId: "navigation",
        contextMenuOrder: 1.1,
        run: function (ed) {
          runDefinition(ed.getPosition());
        },
      });
      editor.addAction({
        id: "livecode.gotoReferences",
        label: "Go to References",
        keybindings: [KM.Shift | KC.F12],
        contextMenuGroupId: "navigation",
        contextMenuOrder: 1.2,
        run: function (ed) {
          runReferences(ed.getPosition());
        },
      });

      editor.onMouseDown(function (e) {
        var oe = e.event;
        if (!(oe && (oe.metaKey || oe.ctrlKey))) return;
        var t = e.target;
        if (!t || t.type !== monaco.editor.MouseTargetType.CONTENT_TEXT || !t.position) return;
        oe.preventDefault();
        oe.stopPropagation();
        runDefinition(t.position);
      });
    }

    function disposePreloaded() {
      if (!window.monaco || !window.monaco.editor) {
        _preloadedUris.clear();
        return;
      }
      _preloadedUris.forEach(function (s) {
        try {
          var m = window.monaco.editor.getModel(window.monaco.Uri.parse(s));
          if (m && !m.isAttachedToEditor()) m.dispose();
        } catch (e) {}
      });
      _preloadedUris.clear();
    }

    window.WBLsp = {
      onProjectOpen: function (nextPath, prevPath) {
        if (!window.monaco) return;
        installProviders();
        if (window.ideEditor) { patchOpenCodeEditor(window.ideEditor); attachNavigation(window.ideEditor); }
        if (activeClient) {
          activeClient.dispose();
          activeClient = null;
        }
        if (prevPath && prevPath !== nextPath) disposePreloaded();
        if (!nextPath) return;
        activeClient = new LspClient(nextPath);
      },

      onFileOpened: function (filePath, model) {
        if (!filePath || !PY_EXT.test(filePath) || !model) return;
        installProviders();
        if (window.ideEditor) { patchOpenCodeEditor(window.ideEditor); attachNavigation(window.ideEditor); }
        if (activeClient) activeClient.openDoc(filePath, model);
      },

      onFileClosed: function (filePath) {
        if (activeClient && filePath) activeClient.closeDoc(filePath);
      },

      disposeAll: function () {
        if (activeClient) {
          activeClient.dispose();
          activeClient = null;
        }
        disposePreloaded();
      },

      _debug: function () {
        return {
          providers: _providersInstalled,
          patched: _openCodeEditorPatched,
          nav: _navAttached,
          client: activeClient
            ? { ready: activeClient.ready, open: Object.keys(activeClient.open), project: activeClient.projectPath }
            : null,
          preloaded: _preloadedUris.size,
        };
      },
    };
  })();
