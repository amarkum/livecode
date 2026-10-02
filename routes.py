from __future__ import annotations

import time
import uuid

from livecode.llm import LLMRouter
from livecode.llm import settings as llm_settings

_TURN_LOCK_WAIT_S = 300

def register_livecode_routes(app, socketio, rt):

    import sys as _sys
    _mod = _sys.modules[__name__]
    for _k, _v in rt.__dict__.items():
        if not _k.startswith("__"):
            setattr(_mod, _k, _v)
    g = rt.__dict__
    jsonify = g["jsonify"]
    request = g["request"]
    Response = g.get("Response")
    stream_with_context = g.get("stream_with_context")

    import os

    from livecode.harness import run_livecode_turn
    from livecode.interjection import enqueue_interjection, is_cancelled, request_cancel, session_turn_lock
    from livecode.session import (
        append_turn_messages,
        append_turn_summary,
        delete_session,
        fork_session,
        list_sessions,
        load_session,
        load_transcript,
        message_index_for_user_turn,
        rename_session,
        rewind_to_message,
        save_transcript,
        session_exists,
        set_session_title,
        _sanitize_display_payload,
    )
    from livecode.workspace import build_workspace_index, index_summary_text, invalidate_workspace_index, workspace_roots
    from livecode.workspace_config import (
        WorkspaceError,
        WorkspaceFolder,
        check_workspace_folders,
        is_livecode_workspace_file,
        load_livecode_workspace,
        merge_workspace_entries,
        validate_workspace_folders,
    )
    from livecode.context_attachments import expand_repo_context_attachments, search_context_targets, list_workspace_skills, resolve_slash_command
    from livecode.project_store import delete_project_storage, workspace_state_path
    from livecode.tools import normalize_mode
    from livecode.pending_changes import (
        keep_pending_changes,
        list_checkpoints,
        list_pending_changes,
        restore_checkpoint,
        undo_pending_changes,
    )
    from livecode.model_pricing import estimate_usage_cost_usd
    from livecode import plan_store

    LIVECODE_LOGGER = g["LIVECODE_LOGGER"]
    create_diff_html = g["create_diff_html"]
    model_supports_multimodal = g["model_supports_multimodal"]
    execute_command_pty = g["execute_command_pty"]
    _repo_grep = g["_repo_grep"]
    _repo_read_file = g["_repo_read_file"]
    _repo_list_dir = g["_repo_list_dir"]
    _repo_ast_symbols = g["_repo_ast_symbols"]

    # Provider keys come from LiveCode Settings > Models (~/.livecode/llm.json), not .env.
    _llm_client = LLMRouter(
        gemini_models=g.get("GEMINI_MODELS") or g.get("AZURE_V3_DEPLOYMENTS"),
        large_content_char_threshold=g.get("LARGE_CONTENT_CHAR_THRESHOLD", 600_000),
        large_file_count_threshold=g.get("LARGE_FILE_COUNT_THRESHOLD", 40),
        max_input_tokens_for_model=lambda m: ((g.get("get_model_max_tokens") or (lambda _m: {}))(m) or {}).get("max_input_tokens"),
    )
    _host_supports_multimodal = model_supports_multimodal

    def model_supports_multimodal(model):
        if _llm_client.supports_model(model):
            return _llm_client.supports_images(model)
        return bool(_host_supports_multimodal and _host_supports_multimodal(model))

    call_azure_openai_non_streaming = _llm_client.complete
    call_azure_openai_with_tools = _llm_client.complete_with_tools
    call_azure_openai_streaming = _llm_client.stream
    is_azure_gpt_model = _llm_client.supports_model

    def _livecode_state_path(project_path, workspace_payload=None):
        return workspace_state_path(project_path, workspace_payload)

    def _livecode_create_diff_html(original_content, new_content, file_ext, csv_delimiter=None):
        return create_diff_html(
            original_content,
            new_content,
            file_ext,
            csv_delimiter,
            max_blocks=None,
            context_lines=2,
        )

    def _livecode_summarize(model: str, messages: list) -> str:
        resolved = (model or "").strip()
        if not resolved or resolved.lower() == "auto":
            try:
                resolved = _llm_client.pick_model(None, task="fast") or resolved
            except Exception:
                resolved = model
        resolved = (resolved or model or "").strip()
        try:
            return call_azure_openai_non_streaming(
                resolved, messages, timeout=(30, 120),
            ) or ""
        except Exception:
            LIVECODE_LOGGER.warning("LiveCode summarize call failed", exc_info=True)
            return ""

    def _livecode_fallback_session_title(question: str) -> str:
        import re

        text = re.sub(r"<[^>]+>", " ", question or "")
        text = re.sub(r"[`*_#>\[\](){}]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" .?!:;-\"'")
        if not text:
            return "New LiveCode Chat"
        words = text.split()
        return " ".join(words[:7]).title()[:80]

    def _livecode_clean_session_title(raw: str, question: str) -> str:
        import re

        title = str(raw or "").strip()
        title = title.splitlines()[0].strip() if title else ""
        title = re.sub(r"^(title|chat title|session title)\s*:\s*", "", title, flags=re.I)
        title = title.strip(" \t\r\n\"'`“”‘’.:;-—")
        title = re.sub(r"\s+", " ", title)
        if not title or len(title.split()) > 10 or len(title) > 80:
            return _livecode_fallback_session_title(question)
        return title[:80]

    def _livecode_generate_session_title(question: str, user_model: str) -> str:
        model = (user_model or "").strip()
        try:
            if not model or model.lower() == "auto":
                model = _llm_client.pick_model(None, task="fast")
        except Exception:
            model = user_model
        model = (model or user_model or "auto").strip()
        messages = [
            {
                "role": "system",
                "content": (
                    "Generate a concise, polished title for an LiveCode coding chat. "
                    "Use 3 to 7 words. Do not copy the user's wording verbatim. "
                    "Return only the title, with no quotes and no explanation."
                ),
            },
            {"role": "user", "content": question},
        ]
        try:
            raw = call_azure_openai_non_streaming(model, messages, timeout=(10, 30)) or ""
            return _livecode_clean_session_title(raw, question)
        except Exception:
            LIVECODE_LOGGER.warning("LiveCode session title generation failed", exc_info=True)
            return _livecode_fallback_session_title(question)

    def _index_details(index: dict, symbol_stats: dict) -> dict:
        """For Settings > Indexing: size, freshness, what the files are, where they live, and what is skipped."""
        from livecode import workspace as ws
        from livecode.codebase_index import SYMBOL_EXTENSIONS

        files = index.get("files") or []
        dir_counts: dict[str, int] = {}
        for f in files:
            rel = str(f.get("rel") or "")
            top = rel.split("/", 1)[0] + "/" if "/" in rel else ""
            dir_counts[top] = dir_counts.get(top, 0) + 1
        return {
            "total_bytes": sum(int(f.get("size") or 0) for f in files),
            "indexed_at": index.get("indexed_at") or 0,
            "symbols_indexed_at": symbol_stats.get("indexed_at") or 0,
            # [name, count] pairs, largest first (a JSON object would come back sorted by name).
            "ext_counts": sorted((index.get("ext_counts") or {}).items(), key=lambda kv: -kv[1]),
            "dir_counts": sorted(dir_counts.items(), key=lambda kv: -kv[1])[:8],
            "symbol_languages": sorted((symbol_stats.get("languages") or {}).items(), key=lambda kv: -kv[1]),
            "symbol_extensions": sorted(SYMBOL_EXTENSIONS),
            "max_files": ws.MAX_FILES,
            "max_file_bytes": ws.MAX_FILE_SIZE,
            "skip_dirs": sorted(ws.SKIP_DIRS)[:12],
        }

    @app.route("/livecode/index", methods=["POST"])
    def livecode_build_index():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        force = bool(data.get("force"))
        try:
            from livecode.codebase_index import get_codebase_index

            workspace = workspace_roots(expanded, workspace_payload)
            if force:
                invalidate_workspace_index()
            results = [(folder, build_workspace_index(folder.path, force=force)) for folder in workspace.folders]
            symbols = [get_codebase_index(folder.path, force=force).stats() for folder in workspace.folders]
            primary_index = results[0][1]
            return jsonify({
                "success": True,
                "file_count": primary_index.get("file_count", 0),
                "truncated": bool(primary_index.get("truncated")),
                "symbol_count": sum(int(s.get("symbol_count") or 0) for s in symbols),
                "summary": index_summary_text(primary_index, symbol_index=symbols[0] if symbols else None),
                "folders": [
                    {
                        "name": folder.name,
                        "path": folder.path,
                        "file_count": index.get("file_count", 0),
                        "truncated": bool(index.get("truncated")),
                        "from_cache": bool(index.get("from_cache")),
                        "symbol_count": int(sym.get("symbol_count") or 0),
                    }
                    for (folder, index), sym in zip(results, symbols)
                ],
                "missing": [{"name": folder.name, "path": folder.path} for folder in workspace.missing],
                "details": _index_details(primary_index, symbols[0] if symbols else {}),
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode index error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/sessions", methods=["GET", "POST"])
    def livecode_list_sessions():
        data = request.get_json(silent=True) or {} if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        try:
            limit = int(request.args.get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        try:
            sessions = list_sessions(_livecode_state_path(expanded, workspace_payload), limit=min(max(limit, 1), 100))
            return jsonify({"success": True, "sessions": sessions})
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode list sessions error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/session", methods=["GET", "POST"])
    def livecode_load_session():
        data = request.get_json(silent=True) or {} if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        session_id = (data.get("session_id") or request.args.get("session_id") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path or not session_id:
            return jsonify({"error": "project_path and session_id required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        # The chat is shown as it was rendered (transcript.html); message_count says whether there is a
        # history at all, so a chat saved before transcripts existed can say so instead of showing nothing.
        try:
            state_path = _livecode_state_path(expanded, workspace_payload)
            session = load_session(state_path, session_id)
            return jsonify({
                "success": True,
                "session_id": session_id,
                "summary": session.get("summary") or {},
                "transcript_html": load_transcript(state_path, session_id),
                "message_count": len(session.get("messages") or []),
            })
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode load session error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/session/transcript", methods=["POST"])
    def livecode_save_transcript():
        # {project_path, session_id, html, workspace?} -> {success}. success is false (not an error) when the
        # transcript is over the 40 MB cap. 400 when the session is not in that project's workspace: the page
        # only sends a chat to the workspace it belongs to.
        data = request.get_json(silent=True) or {}
        project_path = str(data.get("project_path") or "").strip()
        session_id = str(data.get("session_id") or "").strip()
        html = data.get("html")
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path or not session_id or not isinstance(html, str):
            return jsonify({"error": "project_path, session_id and html required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        try:
            state_path = _livecode_state_path(expanded, workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not session_exists(state_path, session_id):
            return jsonify({"error": "This chat is not in that workspace."}), 400
        try:
            saved = save_transcript(state_path, session_id, html)
        except OSError as e:
            return jsonify({"error": f"Could not save the chat: {e.strerror or e}"}), 500
        return jsonify({"success": bool(saved), **({} if saved else {"reason": "too_large"})})

    @app.route("/livecode/project-storage", methods=["DELETE", "POST"])
    def livecode_delete_project_storage():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        if is_livecode_workspace_file(project_path):
            return jsonify({"error": "Refusing to delete project storage for a workspace file"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            deleted = delete_project_storage(_livecode_state_path(project_path, workspace_payload))
            return jsonify({"success": True, "deleted": deleted})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode delete project storage error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/permission", methods=["POST"])
    def livecode_resolve_permission():
        from livecode.permissions import resolve_permission

        data = request.get_json(silent=True) or {}
        request_id = (data.get("request_id") or "").strip()
        if not request_id:
            return jsonify({"error": "request_id required"}), 400
        approved = bool(data.get("approved"))
        if not resolve_permission(request_id, approved):
            return jsonify({"error": "Unknown or expired permission request"}), 404
        return jsonify({"success": True, "approved": approved})

    @app.route("/livecode/question", methods=["POST"])
    def livecode_answer_question():
        from livecode.questions import resolve_question_request

        data = request.get_json(silent=True) or {}
        request_id = (data.get("request_id") or "").strip()
        if not request_id:
            return jsonify({"error": "request_id required"}), 400
        answers = data.get("answers") if isinstance(data.get("answers"), list) else []
        response = {
            "skipped": bool(data.get("skipped")),
            "answers": answers,
            "details": str(data.get("details") or ""),
        }
        if not resolve_question_request(request_id, response):
            return jsonify({"error": "Unknown or expired question request"}), 404
        return jsonify({"success": True})

    @app.route("/livecode/interject", methods=["POST"])
    def livecode_interject():
        data = request.get_json(silent=True) or {}
        session_id = (data.get("session_id") or "").strip()
        message = (data.get("message") or "").strip()
        if not session_id or not message:
            return jsonify({"error": "session_id and message required"}), 400
        enqueue_interjection(session_id, message)
        return jsonify({"success": True})

    @app.route("/livecode/cancel", methods=["POST"])
    def livecode_cancel_turn():
        data = request.get_json(silent=True) or {}
        session_id = (data.get("session_id") or "").strip()
        if not session_id:
            return jsonify({"error": "session_id required"}), 400
        request_cancel(session_id)
        return jsonify({"success": True})

    @app.route("/livecode/session/fork", methods=["POST"])
    def livecode_fork_session():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        session_id = (data.get("session_id") or "").strip()
        new_session_id = (data.get("new_session_id") or "").strip()
        if not project_path or not session_id or not new_session_id:
            return jsonify({"error": "project_path, session_id, new_session_id required"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            state_path = _livecode_state_path(project_path, workspace_payload)
            session = fork_session(state_path, session_id, new_session_id)
            return jsonify({"success": True, "session_id": new_session_id, "message_count": len(session.get("messages") or [])})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/rewind", methods=["POST"])
    def livecode_rewind_session():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        session_id = (data.get("session_id") or "").strip()
        message_index = data.get("message_index")
        if not project_path or not session_id or message_index is None:
            return jsonify({"error": "project_path, session_id, message_index required"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            state_path = _livecode_state_path(project_path, workspace_payload)
            kept = rewind_to_message(state_path, session_id, int(message_index))
            return jsonify({"success": True, "message_count": len(kept)})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    def _livecode_changes_request():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        session_id = (data.get("session_id") or "").strip()
        if not project_path or not session_id:
            return None, None, None, (jsonify({"error": "project_path and session_id required"}), 400)
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        paths = [str(p) for p in data.get("paths") if p] if isinstance(data.get("paths"), list) else None
        try:
            state_path = _livecode_state_path(project_path, workspace_payload)
        except ValueError as e:
            return None, None, None, (jsonify({"error": str(e), "code": "workspace_invalid"}), 400)
        return state_path, session_id, paths, None

    @app.route("/livecode/session/changes", methods=["POST"])
    def livecode_session_changes():
        state_path, session_id, _paths, error = _livecode_changes_request()
        if error:
            return error
        try:
            return jsonify({"success": True, "files": list_pending_changes(state_path, session_id)})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/changes/keep", methods=["POST"])
    def livecode_keep_session_changes():
        state_path, session_id, paths, error = _livecode_changes_request()
        if error:
            return error
        try:
            return jsonify({"success": True, "kept": keep_pending_changes(state_path, session_id, paths)})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/changes/undo", methods=["POST"])
    def livecode_undo_session_changes():
        state_path, session_id, paths, error = _livecode_changes_request()
        if error:
            return error
        try:
            outcome = undo_pending_changes(state_path, session_id, paths)
            invalidate_workspace_index()
            return jsonify({"success": True, **outcome})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/checkpoints", methods=["POST"])
    def livecode_session_checkpoints():
        state_path, session_id, _paths, error = _livecode_changes_request()
        if error:
            return error
        try:
            return jsonify({"success": True, "checkpoints": list_checkpoints(state_path, session_id)})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/checkpoints/restore", methods=["POST"])
    def livecode_restore_checkpoint():
        data = request.get_json(silent=True) or {}
        state_path, session_id, _paths, error = _livecode_changes_request()
        if error:
            return error
        try:
            user_index = int(data.get("user_index"))
        except (TypeError, ValueError):
            return jsonify({"error": "user_index (0-based position of the user message) required"}), 400
        if user_index < 0:
            return jsonify({"error": "user_index must be >= 0"}), 400
        try:
            outcome = restore_checkpoint(state_path, session_id, user_index)
            invalidate_workspace_index()
            payload = {"success": not outcome["failed"], **outcome}
            if data.get("rewind_chat", True) and not outcome["failed"]:
                messages = load_session(state_path, session_id).get("messages") or []
                index = message_index_for_user_turn(messages, user_index)
                if index is not None:
                    content = messages[index].get("content")
                    display = messages[index].get("display") if isinstance(messages[index].get("display"), dict) else None
                    kept = rewind_to_message(state_path, session_id, index)
                    payload["message_count"] = len(kept)
                    payload["prompt"] = (display or {}).get("text") or (
                        content if isinstance(content, str) else " ".join(
                            str(b.get("text") or "") for b in (content or []) if isinstance(b, dict)
                        )
                    )
            if outcome["failed"]:
                payload["error"] = f"Couldn't restore {len(outcome['failed'])} file(s)"
            return jsonify(payload), (200 if not outcome["failed"] else 409)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/rename", methods=["POST"])
    def livecode_rename_session():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        session_id = (data.get("session_id") or "").strip()
        title = (data.get("title") or "").strip()
        if not project_path or not session_id or not title:
            return jsonify({"error": "project_path, session_id, and title required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            if not rename_session(_livecode_state_path(expanded, workspace_payload), session_id, title):
                return jsonify({"error": "Session not found"}), 404
            return jsonify({"success": True, "session_id": session_id, "title": title[:200]})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/session/delete", methods=["POST"])
    def livecode_delete_session():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        session_id = (data.get("session_id") or "").strip()
        if not project_path or not session_id:
            return jsonify({"error": "project_path and session_id required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            state_path = _livecode_state_path(expanded, workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e), "code": "workspace_invalid"}), 400
        existed = delete_session(state_path, session_id)
        return jsonify({"success": True, "session_id": session_id, "existed": existed})

    @app.route("/livecode/context-search", methods=["GET", "POST"])
    def livecode_context_search():
        data = request.get_json(silent=True) or {} if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        query = (data.get("q") or request.args.get("q") or "").strip()
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        try:
            limit = int(data.get("limit") or request.args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        directory = None
        if "directory" in data:
            directory = str(data.get("directory") or "").strip()
        elif "directory" in request.args:
            directory = (request.args.get("directory") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            payload = search_context_targets(
                expanded,
                query,
                limit=limit,
                directory=directory,
                workspace_payload=workspace_payload,
            )
            return jsonify(payload)
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode context search error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/source-models", methods=["GET"])
    def livecode_source_models():
        project_path = (request.args.get("project") or "").strip()
        if not project_path:
            return jsonify({"error": "project required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400

        raw_exts = (request.args.get("exts") or "ts,tsx,js,jsx").split(",")
        exts = tuple("." + e.strip().lstrip(".").lower() for e in raw_exts if e.strip())
        if not exts:
            return jsonify({"error": "exts required"}), 400

        MAX_FILES = 1500
        MAX_TOTAL_BYTES = 8 * 1024 * 1024
        MAX_FILE_BYTES = 512 * 1024
        SKIP_DIRS = {
            "node_modules", ".git", ".hg", ".svn", "dist", "build", ".next",
            ".nuxt", "out", "coverage", ".turbo", ".cache", "__pycache__",
            ".venv", "venv", ".mypy_cache", ".pytest_cache",
        }

        files = []
        total = 0
        truncated = False
        for root, dirs, names in os.walk(expanded):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                if not name.lower().endswith(exts):
                    continue
                fpath = os.path.join(root, name)
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                if size > MAX_FILE_BYTES:
                    continue
                if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
                    truncated = True
                    break
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                except OSError:
                    continue
                files.append({"path": fpath, "content": content})
                total += size
            if truncated:
                break

        tsconfig = None
        for cfg_name in ("tsconfig.json", "jsconfig.json"):
            cfg_path = os.path.join(expanded, cfg_name)
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8", errors="ignore") as fh:
                        tsconfig = fh.read()
                except OSError:
                    pass
                break

        return jsonify({
            "project": expanded,
            "files": files,
            "count": len(files),
            "truncated": truncated,
            "tsconfig": tsconfig,
        })

    @app.route("/livecode/fx-rate", methods=["GET"])
    def livecode_fx_rate():
        from livecode.fx_rate import get_usd_to_inr_rate
        return jsonify(get_usd_to_inr_rate())

    @app.route("/livecode/skills", methods=["GET", "POST"])
    def livecode_skills():
        data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        try:
            skills = list_workspace_skills(expanded, workspace_payload)
            return jsonify({"success": True, "skills": skills})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            LIVECODE_LOGGER.exception("livecode skills list error")
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/plans", methods=["GET"])
    def livecode_list_plans():
        project_path = (request.args.get("project_path") or "").strip()
        try:
            limit = max(1, min(int(request.args.get("limit", 200)), 500))
        except (TypeError, ValueError):
            limit = 200
        try:
            plans = plan_store.list_plans(limit=limit, project_path=project_path)
            return jsonify({"ok": True, "plans": plans})
        except OSError as e:
            LIVECODE_LOGGER.exception("livecode list plans error")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/livecode/plan-content", methods=["GET"])
    def livecode_plan_content():
        filename = (request.args.get("file") or "").strip()
        try:
            plan = plan_store.read_plan(filename)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "plan not found"}), 404
        except OSError as e:
            LIVECODE_LOGGER.exception("livecode plan content error")
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({
            "ok": True,
            "file": plan["file"],
            "title": plan["title"],
            "overview": plan["overview"],
            "todos": plan["todos"],
            "content": plan["body"],
            "raw": plan["content"],
            "meta": plan["meta"],
        })

    @app.route("/livecode/plan/save", methods=["POST"])
    def livecode_plan_save():
        data = request.get_json(silent=True) or {}
        filename = (data.get("file") or "").strip()
        if "content" not in data:
            return jsonify({"ok": False, "error": "content required"}), 400
        content = data.get("content")
        if content is None:
            content = ""
        content = str(content)
        try:
            existing = plan_store.read_plan(filename)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "plan not found"}), 404
        except OSError as e:
            LIVECODE_LOGGER.exception("livecode plan save read error")
            return jsonify({"ok": False, "error": str(e)}), 500
        meta = existing.get("meta") or {}
        title = (data.get("title") or existing.get("title") or "").strip() or "Untitled plan"
        try:
            saved = plan_store.write_plan(
                content,
                title=title,
                project_path=meta.get("project_path") or "",
                session_id=meta.get("session_id") or "",
                filename=existing["file"],
            )
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OSError as e:
            LIVECODE_LOGGER.exception("livecode plan save error")
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({
            "ok": True,
            "file": saved["file"],
            "title": saved["title"],
            "content": saved["body"],
            "todos": saved["todos"],
        })

    @app.route("/livecode/plan/save-to-workspace", methods=["POST"])
    def livecode_plan_save_to_workspace():
        data = request.get_json(silent=True) or {}
        filename = (data.get("file") or "").strip()
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"ok": False, "error": "project_path required"}), 400
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"ok": False, "error": f"Project path not found: {project_path}"}), 400
        try:
            plan = plan_store.read_plan(filename)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "plan not found"}), 404
        dest_dir = os.path.join(expanded, "docs", "plans")
        dest = os.path.join(dest_dir, plan["file"])
        try:
            os.makedirs(dest_dir, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(plan["body"] + "\n")
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({
            "ok": True,
            "file": plan["file"],
            "path": dest,
            "relative_path": os.path.relpath(dest, expanded).replace("\\", "/"),
        })

    @app.route("/livecode/plan/delete", methods=["POST"])
    def livecode_plan_delete():
        data = request.get_json(silent=True) or {}
        filename = (data.get("file") or "").strip()
        try:
            deleted = plan_store.delete_plan(filename)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        if not deleted:
            return jsonify({"ok": False, "error": "plan not found"}), 404
        return jsonify({"ok": True, "file": filename})

    def _livecode_workspace_response(path_or_project, allow_partial=False):
        workspace = load_livecode_workspace(path_or_project, allow_partial=allow_partial)
        return {
            "path": workspace.config_path or workspace.primary_path,
            "primary_path": workspace.primary_path,
            "folders": [
                {"name": folder.name, "path": folder.path}
                for folder in workspace.folders
            ],
            "missing": [
                {"name": folder.name, "path": folder.path}
                for folder in workspace.missing
            ],
            "settings": workspace.settings,
            "mcpServers": workspace.mcp_servers,
        }

    def _livecode_valid_workspace_ref(project_path):
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if os.path.isdir(expanded):
            return expanded
        if os.path.isfile(expanded) and is_livecode_workspace_file(expanded):
            return expanded
        raise ValueError(f"Project path not found: {project_path}")

    @app.route("/livecode/workspace/load", methods=["POST"])
    def livecode_workspace_load():
        data = request.get_json(silent=True) or {}
        workspace_path = (data.get("workspace_path") or "").strip()
        if not workspace_path:
            return jsonify({"error": "workspace_path required"}), 400
        try:
            workspace = _livecode_workspace_response(workspace_path, allow_partial=bool(data.get("allow_partial")))
            return jsonify({"success": True, "workspace": workspace})
        except WorkspaceError as e:
            payload = {"error": str(e), "missing": [{"name": f.name, "path": f.path} for f in e.missing]}
            if e.missing and e.remaining > 0:
                payload["code"] = "workspace_folders_missing"
                payload["remaining"] = e.remaining
                return jsonify(payload), 409
            return jsonify(payload), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/livecode/workspace/save", methods=["POST"])
    def livecode_workspace_save():
        import json as _json
        import tempfile as _tempfile
        data = request.get_json(silent=True) or {}
        workspace_path = (data.get("workspace_path") or "").strip()
        folders = data.get("folders") or []
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        mcp_servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
        if not workspace_path:
            return jsonify({"error": "workspace_path required"}), 400
        if not isinstance(folders, list) or not folders:
            return jsonify({"error": "At least one workspace folder is required"}), 400
        target_input = os.path.expanduser(workspace_path)
        if not os.path.isabs(target_input):
            return jsonify({"error": "Workspace file path must be absolute or start with ~"}), 400
        expanded = os.path.abspath(target_input)
        if not is_livecode_workspace_file(expanded):
            expanded += ".livecode-workspace.json"
        folder_models = []
        for item in folders:
            raw_path = item.get("path") if isinstance(item, dict) else item
            folder_input = os.path.expanduser(str(raw_path or "").strip())
            if not folder_input or not os.path.isabs(folder_input):
                return jsonify({"error": f"Workspace folder path must be absolute: {raw_path}"}), 400
            folder_path = os.path.abspath(folder_input)
            if not os.path.isdir(folder_path):
                return jsonify({"error": f"Workspace folder not found: {raw_path}"}), 400
            raw_name = item.get("name") if isinstance(item, dict) else ""
            name = str(raw_name or "").strip() or os.path.basename(folder_path) or "workspace"
            folder_models.append(WorkspaceFolder(name=name, path=folder_path))
        try:
            validate_workspace_folders(folder_models)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        existing = {}
        if os.path.isfile(expanded):
            try:
                with open(expanded, "r", encoding="utf-8") as fh:
                    loaded = _json.load(fh)
            except (OSError, ValueError):
                loaded = None
            if not isinstance(loaded, dict):
                return jsonify({"error": f"Refusing to overwrite a file that is not a workspace JSON object: {expanded}"}), 400
            existing = loaded
        doc = dict(existing)
        doc["folders"] = merge_workspace_entries(existing.get("folders"), folder_models, os.path.dirname(expanded))
        doc["settings"] = settings
        doc["mcpServers"] = mcp_servers
        try:
            target_dir = os.path.dirname(expanded) or "."
            os.makedirs(target_dir, exist_ok=True)
            fd, tmp_path = _tempfile.mkstemp(prefix=".livecode-workspace-", suffix=".tmp", dir=target_dir, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    _json.dump(doc, fh, indent=2)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, expanded)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return jsonify({"success": True, "workspace": _livecode_workspace_response(expanded)})
        except OSError as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/livecode/workspace/validate", methods=["POST"])
    def livecode_workspace_validate():
        data = request.get_json(silent=True) or {}
        raw_folders = data.get("folders")
        if not isinstance(raw_folders, list) or not raw_folders:
            return jsonify({"error": "folders must be a non-empty array"}), 400
        present, missing = check_workspace_folders(raw_folders)
        return jsonify({
            "success": True,
            "folders": [{"name": folder.name, "path": folder.path} for folder in present],
            "missing": missing,
            "primary_path": present[0].path if present else "",
        })

    @app.route("/livecode-mcp/status", methods=["GET", "POST"])
    def livecode_mcp_status():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {} if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        enabled = data.get("enabled_servers") if isinstance(data.get("enabled_servers"), list) else None
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        try:
            expanded = _livecode_valid_workspace_ref(project_path)
            if workspace_payload is not None:
                mcp_bridge.workspace_from_payload(expanded, workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(mcp_bridge.get_mcp_status(expanded, enabled_servers=None, workspace_payload=workspace_payload, probe_servers=enabled))

    @app.route("/livecode-mcp/refresh", methods=["POST"])
    def livecode_mcp_refresh():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        enabled = data.get("enabled_servers") if isinstance(data.get("enabled_servers"), list) else None
        expanded = os.path.abspath(os.path.expanduser(project_path)) if project_path else ""
        try:
            if expanded and workspace_payload is not None:
                mcp_bridge.workspace_from_payload(expanded, workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        mcp_bridge.refresh_mcp_cache(expanded or None, workspace_payload)
        if not expanded:
            return jsonify({"success": True})
        return jsonify(mcp_bridge.get_mcp_status(expanded, enabled_servers=None, workspace_payload=workspace_payload, probe_servers=enabled))

    @app.route("/livecode-mcp/diagnostics", methods=["POST"])
    def livecode_mcp_diagnostics():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        server_name = (data.get("server_name") or "").strip() or None
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path:
            return jsonify(error="project_path required"), 400
        try:
            expanded = _livecode_valid_workspace_ref(project_path)
            if workspace_payload is not None:
                mcp_bridge.workspace_from_payload(expanded, workspace_payload)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        payload = mcp_bridge.get_mcp_server_diagnostics(expanded, server_name, workspace_payload)
        code = 200 if payload.get("success") else 404
        return jsonify(payload), code

    @app.route("/livecode-mcp/action", methods=["POST"])
    def livecode_mcp_action():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify(error="project_path required"), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        action = data.get("action") or "reload_config"
        server_name = (data.get("server_name") or "").strip() or None
        enabled = data.get("enabled_servers") if isinstance(data.get("enabled_servers"), list) else None
        try:
            expanded = _livecode_valid_workspace_ref(project_path)
            if workspace_payload is not None:
                mcp_bridge.workspace_from_payload(expanded, workspace_payload)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(mcp_bridge.lifecycle_mcp_servers(expanded, action, server_name, enabled, workspace_payload))

    @app.route("/livecode/rules", methods=["GET", "POST"])
    def livecode_rules():
        from livecode.rules import discover_project_rules, file_identity
        data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
        project_path = (data.get("project_path") or request.args.get("project_path") or "").strip()
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        try:
            workspace = workspace_roots(os.path.abspath(os.path.expanduser(project_path)), workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        files = []
        seen = set()
        for folder in workspace.folders:
            for rule in discover_project_rules(folder.path):
                ident = file_identity(rule.file_path)
                if ident in seen:
                    continue
                seen.add(ident)
                files.append({
                    "name": rule.file_name,
                    "path": rule.file_path,
                    "folder": folder.name,
                    "chars": len(rule.content),
                })
        return jsonify({"success": True, "files": files, "primary_path": workspace.primary_path})

    @app.route("/livecode/memory", methods=["GET", "POST"])
    def livecode_memory():
        # {success, files: [{path, name, source, size, modified, exists}]}: MEMORY.md, then session logs newest first.
        from livecode.memory import list_editable_memory
        data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
        try:
            state_path = _memory_state_path(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            return jsonify({"success": True, "files": list_editable_memory(state_path)})
        except OSError as e:
            return jsonify({"error": f"Could not list memory files: {e.strerror or e}"}), 500

    @app.route("/livecode/rules/create", methods=["POST"])
    def livecode_rules_create():
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        expanded = os.path.abspath(os.path.expanduser(project_path)) if project_path else ""
        if not expanded or not os.path.isdir(expanded):
            return jsonify({"error": "Open a project first."}), 400
        target = os.path.join(expanded, "AGENTS.md")
        try:
            with open(target, "x", encoding="utf-8") as fh:
                fh.write(
                    "# Agent instructions\n\n"
                    "Rules the LiveCode agent follows in this project.\n\n"
                    "## Commands\n\n- Test: \n- Lint: \n\n"
                    "## Conventions\n\n- \n"
                )
        except FileExistsError:
            pass
        invalidate_workspace_index()
        return jsonify({"success": True, "path": target})

    def _livecode_client_enabled_servers(data):
        raw = data.get("enabled_servers")
        return {name for name in raw if isinstance(name, str) and name} if isinstance(raw, list) else set()

    @app.route("/livecode-mcp/servers/add", methods=["POST"])
    def livecode_mcp_add_server():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"success": False, "error": "project_path required"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        spec = data.get("server") if isinstance(data.get("server"), dict) else {}
        try:
            expanded = _livecode_valid_workspace_ref(project_path)
            result = mcp_bridge.add_mcp_server(
                expanded,
                str(data.get("name") or ""),
                spec,
                scope=str(data.get("scope") or "project"),
                workspace_payload=workspace_payload,
                overwrite=bool(data.get("overwrite")),
            )
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        if not result.get("success"):
            return jsonify(result), 400
        probe = _livecode_client_enabled_servers(data) | {result["name"]}
        status = mcp_bridge.get_mcp_status(expanded, enabled_servers=None, workspace_payload=workspace_payload, probe_servers=sorted(probe))
        return jsonify({**result, "status": status})

    @app.route("/livecode-mcp/servers/remove", methods=["POST"])
    def livecode_mcp_remove_server():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"success": False, "error": "project_path required"}), 400
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            expanded = _livecode_valid_workspace_ref(project_path)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        result = mcp_bridge.remove_mcp_server(expanded, str(data.get("name") or ""), workspace_payload)
        if not result.get("success"):
            return jsonify(result), 400
        probe = _livecode_client_enabled_servers(data) - {result["name"]}
        status = mcp_bridge.get_mcp_status(expanded, enabled_servers=None, workspace_payload=workspace_payload, probe_servers=sorted(probe))
        return jsonify({**result, "status": status})

    _livecode_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    _livecode_assets_dir = os.path.join(_livecode_static_dir, "assets")

    @app.route("/livecode/assets/<path:filename>", methods=["GET"])
    def livecode_asset(filename):
        from flask import send_from_directory
        return send_from_directory(_livecode_assets_dir, filename)

    @app.route("/livecode/static/<path:filename>", methods=["GET"])
    def livecode_static(filename):
        from flask import send_from_directory
        return send_from_directory(_livecode_static_dir, filename)

    @app.route("/livecode/clone", methods=["POST"])
    def livecode_clone_repo():
        import re as _re
        import subprocess as _subprocess
        data = request.get_json(silent=True) or {}
        url = str(data.get("url") or "").strip()
        parent = os.path.abspath(os.path.expanduser(str(data.get("parent_dir") or "~").strip() or "~"))
        if not url or url.startswith("-") or not _re.match(r"^(https?://|ssh://|git://|git@|file://|/|~)", url):
            return jsonify({"success": False, "error": "Enter a git URL (https://…, git@host:org/repo.git, or ssh://…)."}), 400
        if not os.path.isdir(parent):
            return jsonify({"success": False, "error": f"Folder not found: {parent}"}), 400
        name = str(data.get("name") or "").strip()
        if not name:
            name = _re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "repo"
        if name.startswith(".") or "/" in name or "\\" in name:
            return jsonify({"success": False, "error": "Choose a plain folder name for the clone."}), 400
        target = os.path.join(parent, name)
        if os.path.exists(target):
            return jsonify({"success": False, "error": f"{target} already exists."}), 409
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            proc = _subprocess.run(
                ["git", "clone", "--", os.path.expanduser(url) if url.startswith("~") else url, target],
                capture_output=True, text=True, timeout=600, env=env,
            )
        except FileNotFoundError:
            return jsonify({"success": False, "error": "git is not installed."}), 500
        except _subprocess.TimeoutExpired:
            return jsonify({"success": False, "error": "git clone took longer than 10 minutes and was stopped."}), 504
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return jsonify({"success": False, "error": detail[-1] if detail else "git clone failed."}), 400
        return jsonify({"success": True, "path": target})

    @app.route("/livecode/new-project", methods=["POST"])
    def livecode_new_project():
        data = request.get_json(silent=True) or {}
        parent = os.path.abspath(os.path.expanduser(str(data.get("parent_dir") or "~").strip() or "~"))
        if not os.path.isdir(parent):
            return jsonify({"success": False, "error": f"Folder not found: {parent}"}), 400
        name = str(data.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "Enter a project name."}), 400
        if name.startswith(".") or "/" in name or "\\" in name:
            return jsonify({"success": False, "error": "Choose a plain folder name for the project."}), 400
        target = os.path.join(parent, name)
        if os.path.exists(target):
            return jsonify({"success": False, "error": f"{target} already exists."}), 409
        os.makedirs(target)
        return jsonify({"success": True, "path": target})

    @app.route("/livecode-mcp/config", methods=["POST"])
    def livecode_mcp_config():
        from livecode import mcp_bridge
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return jsonify({"error": "project_path required"}), 400
        try:
            path = mcp_bridge.ensure_project_mcp_config(project_path)
            return jsonify({"success": True, "path": path})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": str(e)}), 500

    from livecode import browser as livecode_browser
    from livecode import browser_stream as livecode_frames

    _PANEL_BROWSER_ACTIONS = frozenset({
        "navigate", "back", "forward", "reload", "new_tab", "switch_tab", "close_tab", "tabs",
        "click_point", "scroll", "type_text", "press", "resize", "crop", "inspect_point", "find", "zoom",
    })

    def _browser_target(data):
        project_path = (data.get("project_path") or "").strip()
        if not project_path:
            return None, (jsonify({"error": "Open a project to use the browser."}), 400)
        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return None, (jsonify({"error": f"Project path not found: {project_path}"}), 400)
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        try:
            return _livecode_state_path(expanded, workspace_payload), None
        except ValueError as e:
            return None, (jsonify({"error": str(e)}), 400)

    def _browser_failure(exc):
        if isinstance(exc, livecode_browser.BrowserUnavailable):
            return jsonify({"error": str(exc), "unavailable": True}), 503
        if isinstance(exc, livecode_browser.BrowserError):
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, TimeoutError):
            return jsonify({"error": "The browser did not answer in time."}), 504
        LIVECODE_LOGGER.exception("livecode browser error")
        message = str(exc).strip().splitlines()
        return jsonify({"error": (message[0] if message else exc.__class__.__name__)[:300]}), 500

    def _browser_extras():
        from livecode import figma as livecode_figma

        return {
            "local_cookie_import": livecode_browser.local_cookie_import_available(),
            "local_browsers": list(livecode_browser.LOCAL_BROWSERS),
            "devices": {name: list(size) for name, size in livecode_browser.DEVICE_PRESETS.items()},
            "connection": livecode_browser.connection_status(),
            "figma": livecode_figma.token_status(),
        }

    @app.route("/livecode/browser/status", methods=["POST"])
    def livecode_browser_status():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.status(state_path), **_browser_extras()})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/action", methods=["POST"])
    def livecode_browser_action():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        action = str(data.get("action") or "").strip().lower()
        if action not in _PANEL_BROWSER_ACTIONS:
            return jsonify({"error": f"Unknown browser action: {action or '(none)'}"}), 400
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        if action == "resize":
            args.setdefault("mode", "fit")
        try:
            result = livecode_browser.perform(state_path, action, args, live=bool(data.get("live")))
        except Exception as e:
            return _browser_failure(e)
        result.pop("snapshot", None)
        if result.get("shot_id"):
            result["shot_url"] = livecode_browser.shot_url(result.get("storage_key") or "", result["shot_id"])
        return jsonify({"success": True, **result})

    @app.route("/livecode/browser/frame", methods=["POST"])
    def livecode_browser_frame():
        import base64 as _base64

        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            since = int(data.get("since", -1))
        except (TypeError, ValueError):
            since = -1
        try:
            seq, jpeg, state = livecode_browser.frame(state_path, since, image=data.get("image") is not False)
        except Exception as e:
            return _browser_failure(e)
        out = {"success": True, **state, "seq": seq}
        if jpeg is not None:
            out["frame"] = "data:image/jpeg;base64," + _base64.b64encode(jpeg).decode("ascii")
        return jsonify(out)

    @app.route("/livecode/browser/stream", methods=["GET"])
    def livecode_browser_stream():
        import json as _json

        try:
            workspace = _json.loads(request.args.get("workspace") or "null")
        except ValueError:
            workspace = None
        target = {"project_path": request.args.get("project_path") or "", "workspace": workspace}
        state_path, failed = _browser_target(target)
        if failed:
            return failed
        if request.args.get("w") and request.args.get("h"):
            livecode_browser.set_view_box(state_path, request.args.get("w"), request.args.get("h"))
        try:
            stream = livecode_browser.stream_open(state_path)
        except Exception as e:
            return _browser_failure(e)
        if stream is None:
            return jsonify({"error": "There is no page to stream yet."}), 409
        response = Response(livecode_frames.mjpeg(stream),
                            mimetype="multipart/x-mixed-replace; boundary=" + livecode_frames.BOUNDARY,
                            direct_passthrough=True)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.route("/livecode/browser/inspect-map", methods=["POST"])
    def livecode_browser_inspect_map():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.inspect_map(state_path)})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/view", methods=["POST"])
    def livecode_browser_view():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        livecode_browser.set_view_box(state_path, data.get("w"), data.get("h"))
        return jsonify({"success": True})

    @app.route("/livecode/browser/input", methods=["POST"])
    def livecode_browser_input():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            reply = livecode_browser.input_events(state_path, data.get("events"))
        except Exception as e:
            return _browser_failure(e)
        return jsonify({"success": True, **reply})

    try:
        from flask_sock import Sock as _BrowserSock
    except Exception:
        _BrowserSock = None
    if _BrowserSock is not None:
        _browser_sock = _BrowserSock(app)

        @_browser_sock.route("/livecode/browser/input/ws")
        def livecode_browser_input_ws(ws):
            import json as _json

            try:
                workspace = _json.loads(request.args.get("workspace") or "null")
            except ValueError:
                workspace = None
            state_path, failed = _browser_target({"project_path": request.args.get("project_path") or "", "workspace": workspace})
            if failed:
                ws.close(1008, "Open a project to use the browser.")
                return
            while True:
                message = ws.receive(timeout=30)
                events = []
                while message is not None and len(events) < 400:
                    try:
                        parsed = _json.loads(message)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, list):
                        events.extend(item for item in parsed if isinstance(item, dict))
                    elif isinstance(parsed, dict):
                        events.append(parsed)
                    message = ws.receive(timeout=0)
                if not events:
                    continue
                try:
                    reply = livecode_browser.input_events(state_path, events)
                except Exception as e:
                    text = str(e).strip().splitlines()
                    reply = {"ok": False, "error": (text[0] if text else e.__class__.__name__)[:200]}
                ids = [event.get("i") for event in events if isinstance(event.get("i"), int)]
                if ids:
                    reply["i"] = max(ids)
                ws.send(_json.dumps(reply))

    @app.route("/livecode/browser/reference", methods=["POST"])
    def livecode_browser_reference():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            if data.get("get"):
                return jsonify({"success": True, **(livecode_browser.panel_reference(state_path) or {})})
            if data.get("clear"):
                livecode_browser.clear_panel_reference(state_path)
                return jsonify({"success": True, "cleared": True})
            return jsonify({"success": True, **livecode_browser.set_panel_reference(state_path, str(data.get("image") or ""), str(data.get("name") or ""))})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/figma/token", methods=["GET", "POST"])
    def livecode_figma_token():
        from livecode import figma as livecode_figma

        if request.method == "GET":
            return jsonify({"success": True, **livecode_figma.token_status()})
        data = request.get_json(silent=True) or {}
        try:
            status = livecode_figma.save_token("" if data.get("clear") else str(data.get("token") or ""))
        except livecode_figma.FigmaError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not save the token: {e.strerror or e}"}), 500
        return jsonify({"success": True, **status})

    @app.route("/livecode/llm/settings", methods=["GET", "POST"])
    def livecode_llm_settings():
        if request.method == "GET":
            return jsonify({"success": True, **llm_settings.status()})
        try:
            return jsonify({"success": True, **llm_settings.update(request.get_json(silent=True) or {})})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not save the setting: {e.strerror or e}"}), 500

    @app.route("/livecode/llm/test", methods=["POST"])
    def livecode_llm_test():
        model = str((request.get_json(silent=True) or {}).get("model") or "").strip()
        try:
            reply = call_azure_openai_non_streaming(model, [{"role": "user", "content": "Reply with the single word: ok"}], timeout=(10, 60))
        except Exception as e:
            return jsonify({"success": False, "error": str(e)[:500]})
        return jsonify({"success": True, "reply": (reply or "").strip()[:200]})

    @app.route("/livecode/browser/settings", methods=["GET", "POST"])
    def livecode_browser_settings():
        # GET/POST reply: {success, design_accuracy, default_design_accuracy, min_design_accuracy, match_threshold,
        # default_match_threshold, reduce_automation_signals, design_gate, compare_content, ui_verify, agent_tabs, view_quality,
        # default_viewport, allowed}. POST takes any of those setting keys (match_threshold from older clients is saved as the
        # design accuracy it stands for); every value is checked before any is saved.
        if request.method == "GET":
            return jsonify({"success": True, **livecode_browser.browser_settings()})
        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"success": True, **livecode_browser.save_browser_settings(data)})
        except livecode_browser.BrowserError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not save the setting: {e.strerror or e}"}), 500

    from livecode import settings_store

    @app.route("/livecode/settings", methods=["GET", "POST"])
    def livecode_settings():
        # GET: {success, settings}. POST {settings: {key: value | null}} merges them (null removes a key) and
        # replies {success, settings, rejected: {key: reason}}; valid keys are saved even when others are rejected.
        if request.method == "GET":
            return jsonify({"success": True, "settings": settings_store.load_settings()})
        data = request.get_json(silent=True) or {}
        updates = data.get("settings")
        if not isinstance(updates, dict):
            return jsonify({"error": "settings must be an object"}), 400
        try:
            stored, rejected = settings_store.save_settings(updates)
        except OSError as e:
            return jsonify({"error": f"Could not save settings: {e.strerror or e}"}), 500
        return jsonify({"success": True, "settings": stored, "rejected": rejected})

    @app.route("/livecode/settings/reset", methods=["POST"])
    def livecode_settings_reset():
        # Reset all: LiveCode's settings file and the browser and design preferences (the attached Chrome stays).
        data = request.get_json(silent=True) or {}
        try:
            settings_store.reset_settings()
            browser = livecode_browser.browser_settings() if data.get("keep_browser") else livecode_browser.reset_browser_settings()
        except OSError as e:
            return jsonify({"error": f"Could not reset settings: {e.strerror or e}"}), 500
        return jsonify({"success": True, "settings": {}, "browser": browser})

    def _browser_connection_update(data):
        """One place for the three ways to change the connection: {launch, port} starts Chrome with remote
        debugging and attaches to it, {cdp_url} attaches to a running one, {disconnect} goes back to the
        built-in browser (and closes a Chrome LiveCode started)."""
        if data.get("launch"):
            return livecode_browser.launch_chrome_and_attach(data.get("port") or 9222)
        return livecode_browser.set_cdp_endpoint("" if data.get("disconnect") else str(data.get("cdp_url") or ""))

    @app.route("/livecode/browser/connection", methods=["GET", "POST"])
    def livecode_browser_connection():
        # Status: {success, engine, endpoint, source?, connected?, version?, managed_launch, pid?, profile_dir?, port?}.
        if request.method == "GET":
            return jsonify({"success": True, **livecode_browser.connection_status()})
        data = request.get_json(silent=True) or {}
        try:
            status = _browser_connection_update(data)
        except Exception as e:
            return _browser_failure(e)
        return jsonify({"success": True, **status})

    @socketio.on("livecode_browser_connection")
    def livecode_browser_connection_socket(data=None):
        # Same as POST /livecode/browser/connection; the reply comes back as livecode_browser_connection_result,
        # tagged with the request_id the client sent.
        data = data if isinstance(data, dict) else {}
        reply = {"request_id": data.get("request_id")}
        try:
            reply.update({"success": True, **_browser_connection_update(data)})
        except Exception as e:
            body, status = _browser_failure(e)
            reply.update({"success": False, "status": status, **(body.get_json() or {})})
        socketio.emit("livecode_browser_connection_result", reply, to=request.sid)
    @app.route("/livecode/browser/cdp/probe", methods=["POST"])
    def livecode_browser_cdp_probe():
        data = request.get_json(silent=True) or {}
        return jsonify({"success": True, **livecode_browser.probe_cdp(str(data.get("cdp_url") or ""))})

    @app.route("/livecode/browser/cdp/detect", methods=["GET"])
    def livecode_browser_cdp_detect():
        return jsonify({"success": True, **livecode_browser.detect_cdp()})

    @app.route("/livecode/browser/cdp/launch", methods=["POST"])
    def livecode_browser_cdp_launch():
        data = request.get_json(silent=True) or {}
        try:
            status = livecode_browser.launch_debug_chrome(data.get("port") or livecode_browser.DEFAULT_DEBUG_PORT)
        except Exception as e:
            return _browser_failure(e)
        return jsonify({"success": True, **status})

    @app.route("/livecode/agent/settings", methods=["GET", "POST"])
    def livecode_agent_settings():
        from livecode import agent_settings

        if request.method == "GET":
            return jsonify({"success": True, **agent_settings.status()})
        try:
            return jsonify({"success": True, **agent_settings.update(request.get_json(silent=True) or {})})
        except agent_settings.SettingsError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not save the setting: {e.strerror or e}"}), 500

    @app.route("/livecode/agent/settings/reset", methods=["POST"])
    def livecode_agent_settings_reset():
        from livecode import agent_settings

        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"success": True, **agent_settings.reset(str(data.get("group") or ""))})
        except agent_settings.SettingsError as e:
            return jsonify({"error": str(e)}), 400
        except OSError as e:
            return jsonify({"error": f"Could not reset the settings: {e.strerror or e}"}), 500

    def _memory_state_path(data):
        project_path = str(data.get("project_path") or request.args.get("project_path") or "").strip()
        if not project_path:
            raise ValueError("Open a project to see its memory.")
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None
        return _livecode_state_path(os.path.abspath(os.path.expanduser(project_path)), workspace_payload)

    def _memory_status(state_path):
        from livecode.memory.storage import list_memory_files, memory_md_path, memory_root, read_memory_md

        files = list_memory_files(state_path)
        sessions = [f for f in files if f.get("source") == "session"]
        size = 0
        for f in files:
            try:
                size += os.path.getsize(f["abs_path"])
            except OSError:
                pass
        memory_md = read_memory_md(state_path)
        return {
            "root": memory_root(state_path, create=False),
            "memory_path": memory_md_path(state_path, create=False),
            "memory_chars": len(memory_md),
            "memory_exists": bool(memory_md),
            "session_logs": len(sessions),
            "recent_logs": [{"name": os.path.basename(f["abs_path"]), "path": f["abs_path"]} for f in sessions[-8:][::-1]],
            "bytes": size,
        }

    @app.route("/livecode/memory/status", methods=["GET", "POST"])
    def livecode_memory_status():
        data = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
        try:
            state_path = _memory_state_path(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"success": True, **_memory_status(state_path)})

    @app.route("/livecode/memory/file", methods=["POST"])
    def livecode_memory_file():
        # Without a path: MEMORY.md's location, creating an empty one so it opens in the editor.
        # With {path} (MEMORY.md or sessions/<log>.md): reads that file; with content too, saves it.
        # 400 for other paths or non-text content, 404 for a missing log, 500 on OS errors.
        from livecode.memory import read_editable_memory, save_memory_file
        from livecode.memory.storage import memory_md_path, write_text_atomic

        data = request.get_json(silent=True) or {}
        try:
            state_path = _memory_state_path(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        rel = str(data.get("path") or "")
        if rel:
            try:
                if data.get("content") is None:
                    return jsonify({"success": True, **read_editable_memory(state_path, rel)})
                return jsonify({"success": True, **save_memory_file(state_path, rel, data.get("content"))})
            except FileNotFoundError as e:
                return jsonify({"error": str(e)}), 404
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            except OSError as e:
                return jsonify({"error": f"Could not {'save' if data.get('content') is not None else 'read'} {rel}: {e.strerror or e}"}), 500
        path = memory_md_path(state_path, create=True)
        if not os.path.isfile(path):
            try:
                write_text_atomic(path, "## Notes\n\n")
            except OSError as e:
                return jsonify({"error": f"Could not create MEMORY.md: {e.strerror or e}"}), 500
        return jsonify({"success": True, "path": path})

    @app.route("/livecode/memory/reindex", methods=["POST"])
    def livecode_memory_reindex():
        from livecode.memory import embed_missing_chunks, reindex_all

        data = request.get_json(silent=True) or {}
        try:
            state_path = _memory_state_path(data)
            totals = reindex_all(state_path)
            embed_missing_chunks(state_path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            LIVECODE_LOGGER.warning("memory reindex failed", exc_info=True)
            return jsonify({"error": f"Could not rebuild the memory index: {e}"}), 500
        return jsonify({"success": True, "totals": totals, **_memory_status(state_path)})

    @app.route("/livecode/memory/consolidate", methods=["POST"])
    def livecode_memory_consolidate():
        from livecode.memory import run_consolidation

        data = request.get_json(silent=True) or {}
        try:
            state_path = _memory_state_path(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            model = _llm_client.pick_model(None, task="fast")
        except Exception as e:
            return jsonify({"error": f"Add a model in Settings > Models first ({e})."}), 400
        try:
            result = run_consolidation(
                state_path,
                model=model,
                call_summarize=lambda m, msgs: call_azure_openai_non_streaming(m, msgs, timeout=(30, 180)) or "",
                min_hours=0,
                min_new_logs=1,
            )
        except Exception as e:
            LIVECODE_LOGGER.warning("memory consolidation failed", exc_info=True)
            return jsonify({"error": f"Consolidation failed: {e}"}), 500
        return jsonify({"success": True, "result": result, **_memory_status(state_path)})

    @app.route("/livecode/memory/clear", methods=["POST"])
    def livecode_memory_clear():
        import shutil

        from livecode.memory.storage import memory_lock, memory_root

        data = request.get_json(silent=True) or {}
        scope = str(data.get("scope") or "all")
        if scope not in ("all", "sessions"):
            return jsonify({"error": "scope is all or sessions."}), 400
        try:
            state_path = _memory_state_path(data)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        root = memory_root(state_path, create=False)
        try:
            with memory_lock(state_path):
                if os.path.isdir(root):
                    target = os.path.join(root, "sessions") if scope == "sessions" else root
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    if scope == "sessions":
                        index = os.path.join(root, "index.sqlite")
                        for suffix in ("", "-wal", "-shm"):
                            if os.path.exists(index + suffix):
                                os.remove(index + suffix)
        except OSError as e:
            return jsonify({"error": f"Could not clear memory: {e.strerror or e}"}), 500
        return jsonify({"success": True, **_memory_status(state_path)})

    @app.route("/livecode/browser/shot/<storage_key>/<shot_file>", methods=["GET"])
    def livecode_browser_shot(storage_key, shot_file):
        shot_id = shot_file[:-4] if shot_file.endswith(".jpg") else ""
        from flask import send_file

        path = livecode_browser.shot_path(storage_key, shot_id)
        if not path:
            return jsonify({"error": "Screenshot not found"}), 404
        response = send_file(path, mimetype="image/jpeg", max_age=604800)
        response.headers["Cache-Control"] = "private, max-age=604800, immutable"
        return response

    @app.route("/livecode/browser/info", methods=["POST"])
    def livecode_browser_info():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.panel_info(state_path, str(data.get("kind") or ""), data.get("args") if isinstance(data.get("args"), dict) else {})})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/download/<storage_key>/<path:file_name>", methods=["GET"])
    def livecode_browser_download(storage_key, file_name):
        from flask import send_file

        path = livecode_browser.download_path(storage_key, file_name)
        if not path:
            return jsonify({"error": "Download not found"}), 404
        return send_file(path, as_attachment=True, download_name=os.path.basename(path))

    @app.route("/livecode/browser/close", methods=["POST"])
    def livecode_browser_close():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            livecode_browser.close(state_path)
        except Exception as e:
            return _browser_failure(e)
        return jsonify({"success": True})

    @app.route("/livecode/browser/cookies", methods=["POST"])
    def livecode_browser_cookies():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.cookie_summary(state_path), **_browser_extras()})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/cookies/import", methods=["POST"])
    def livecode_browser_cookies_import():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        domain = str(data.get("domain") or "").strip()
        skipped = 0
        try:
            if str(data.get("source") or "") == "local":
                browser_name = str(data.get("browser") or "").strip() or livecode_browser.cookie_browser()
                cookies = livecode_browser.read_local_browser_cookies(browser_name, domain)
            else:
                browser_name = livecode_browser.MANUAL_COOKIE_SOURCE
                text = str(data.get("text") or "")
                if len(text) > 5_000_000:
                    return jsonify({"error": "That cookie file is too large."}), 413
                cookies, skipped = livecode_browser.parse_cookies(text, domain=domain)
            if not cookies:
                return jsonify({"error": "No cookies found to import."}), 400
            result = livecode_browser.import_cookies(state_path, cookies, browser_name)
        except Exception as e:
            return _browser_failure(e)
        return jsonify({"success": True, "skipped": skipped, **result})

    @app.route("/livecode/browser/cookies/default", methods=["POST"])
    def livecode_browser_cookies_default():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.set_default_cookie_browser(state_path, data.get("browser"))})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/cookies/clear", methods=["POST"])
    def livecode_browser_cookies_clear():
        data = request.get_json(silent=True) or {}
        state_path, failed = _browser_target(data)
        if failed:
            return failed
        try:
            return jsonify({"success": True, **livecode_browser.clear_cookies(state_path, str(data.get("domain") or ""))})
        except Exception as e:
            return _browser_failure(e)

    @app.route("/livecode/browser/dev-servers", methods=["GET", "POST"])
    def livecode_browser_dev_servers():
        own_port = None
        try:
            own_port = int(str(request.host or "").rsplit(":", 1)[1])
        except (IndexError, ValueError):
            pass
        ports = tuple(port for port in livecode_browser.DEV_PORTS if port != own_port)
        return jsonify({"success": True, "servers": livecode_browser.detect_dev_servers(ports)})

    @app.route("/livecode-agent", methods=["POST"])
    def livecode_agent():
        from app.attachments import (
            build_multimodal_user_content,
            extract_text_from_user_content,
            has_image_attachments,
        )

        requested_at = time.monotonic()
        data = request.get_json(silent=True) or {}
        project_path = (data.get("project_path") or "").strip()
        question = (data.get("question") or "").strip()
        attachments = data.get("attachments") or []
        session_id = (data.get("session_id") or "").strip() or f"livecode_{uuid.uuid4().hex}"
        socket_id = (data.get("socket_id") or "").strip()
        user_model = (data.get("model") or "").strip() or "auto"
        display_payload = _sanitize_display_payload(data.get("display_payload"))
        mode = normalize_mode(data.get("mode"))
        plan_file = (data.get("plan_file") or "").strip()
        if plan_file and not plan_store.plan_exists(plan_file):
            plan_file = ""
        raw_mcp_servers = data.get("mcp_servers") or []
        mcp_servers = [str(name).strip() for name in raw_mcp_servers if str(name).strip()] if isinstance(raw_mcp_servers, list) else None
        raw_disabled_tools = data.get("mcp_disabled_tools")
        mcp_disabled_tools = {
            str(server): [str(tool) for tool in tools if str(tool).strip()]
            for server, tools in raw_disabled_tools.items()
            if isinstance(tools, list)
        } if isinstance(raw_disabled_tools, dict) else None
        workspace_payload = data.get("workspace") if isinstance(data.get("workspace"), dict) else None

        if not project_path:
            return jsonify({"error": "Open a project folder first."}), 400
        if not question and not attachments:
            return jsonify({"error": "question required"}), 400

        expanded = os.path.abspath(os.path.expanduser(project_path))
        if not os.path.isdir(expanded):
            return jsonify({"error": f"Project path not found: {project_path}"}), 400
        try:
            state_path = _livecode_state_path(expanded, workspace_payload)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if has_image_attachments(attachments) and not model_supports_multimodal(user_model):
            return jsonify({
                "error": "No model that reads images is set up, so image attachments cannot be used. "
                         "Add a key in LiveCode Settings > Models for a provider whose models read images.",
            }), 400

        attachments = expand_repo_context_attachments(expanded, attachments, workspace_payload)

        effective_question, _slash_cmd = resolve_slash_command(expanded, question, workspace_payload)

        user_content = build_multimodal_user_content(effective_question, attachments)
        question_text = extract_text_from_user_content(user_content) or effective_question
        title_seed = question or question_text

        import json as _json

        progress_seq = 0

        def _emit_aux_usage(model_name: str, response: dict | None):
            nonlocal progress_seq
            if not isinstance(response, dict):
                return
            usage = {
                "prompt_tokens": int(response.get("prompt_tokens") or 0),
                "completion_tokens": int(response.get("completion_tokens") or 0),
                "cached_tokens": int(response.get("cached_tokens") or 0),
            }
            cost_usd = estimate_usage_cost_usd(model_name, **usage)
            if cost_usd <= 0:
                return
            progress_seq += 1
            payload = {
                "session_id": session_id,
                "status": "progress",
                "type": "usage",
                "message": "",
                "seq": progress_seq,
                "cost_usd": cost_usd,
                **usage,
            }
            if socket_id:
                socketio.emit("livecode_progress", payload, room=socket_id)
            else:
                socketio.emit("livecode_progress", payload)

        def _call_text_with_usage(model_name: str, messages_: list, timeout=None) -> str:
            try:
                response = call_azure_openai_with_tools(
                    model_name,
                    messages_,
                    [],
                    max_completion_tokens=4096,
                    tool_choice="none",
                )
                _emit_aux_usage(model_name, response)
                return str((response or {}).get("content") or "")
            except Exception:
                if timeout is None:
                    return call_azure_openai_non_streaming(model_name, messages_) or ""
                return call_azure_openai_non_streaming(model_name, messages_, timeout=timeout) or ""

        def _summarize_with_usage(model_name: str, messages_: list) -> str:
            return _call_text_with_usage(model_name, messages_, timeout=(30, 120))

        def _generate_title_with_usage(question_: str, user_model_: str) -> str:
            model = (user_model_ or "").strip()
            try:
                if not model or model.lower() == "auto":
                    model = _llm_client.pick_model(None, task="fast")
            except Exception:
                model = user_model_
            model = (model or user_model_ or "auto").strip()
            title_messages = [
                {
                    "role": "system",
                    "content": (
                        "Generate a concise, polished title for an LiveCode coding chat. "
                        "Use 3 to 7 words. Do not copy the user's wording verbatim. "
                        "Return only the title, with no quotes and no explanation."
                    ),
                },
                {"role": "user", "content": question_},
            ]
            try:
                raw = _call_text_with_usage(model, title_messages, timeout=(10, 30))
                return _livecode_clean_session_title(raw, question_)
            except Exception:
                LIVECODE_LOGGER.warning("LiveCode session title generation failed", exc_info=True)
                return _livecode_fallback_session_title(question_)

        def _run_turn():
            answer_parts = []
            turn_summary = ""
            turn_messages: list = []
            session_title = ""
            should_generate_title = False
            existing_summary: dict = {}
            error_message = ""
            try:
                existing_session = load_session(state_path, session_id)
                existing_summary = existing_session.get("summary") or {}
                existing_messages = existing_session.get("messages") or []
                should_generate_title = not existing_summary.get("title") and not existing_messages
            except Exception:
                should_generate_title = False

            if not existing_summary.get("title"):
                set_session_title(
                    state_path,
                    session_id,
                    _livecode_fallback_session_title(title_seed),
                    overwrite=False,
                )

            for chunk in run_livecode_turn(
                expanded,
                question_text,
                [],
                user_content=user_content,
                user_model=user_model,
                call_with_tools=call_azure_openai_with_tools,
                call_streaming=call_azure_openai_streaming,
                call_summarize=_summarize_with_usage,
                is_azure_model=is_azure_gpt_model,
                repo_grep_fn=_repo_grep,
                repo_read_fn=_repo_read_file,
                repo_list_fn=_repo_list_dir,
                repo_ast_fn=_repo_ast_symbols,
                create_diff_html_fn=_livecode_create_diff_html,
                execute_command_pty_fn=execute_command_pty,
                socketio=socketio,
                session_id=session_id,
                socket_id=socket_id,
                logger=LIVECODE_LOGGER,
                force_reindex=bool(data.get("force_reindex")),
                require_permissions=bool(data.get("require_permissions")),
                enable_mcp_tools=bool(data.get("enable_mcp_tools")),
                mcp_servers=mcp_servers,
                workspace_payload=workspace_payload,
                enable_web_tools=(
                    bool(data.get("enable_web_tools"))
                    if data.get("enable_web_tools") is not None
                    else None
                ),
                enable_browser_tools=bool(data.get("enable_browser_tools")),
                supports_images_fn=model_supports_multimodal,
                mode=mode,
                plan_file=plan_file or None,
                cancel_since=requested_at,
                mcp_disabled_tools=mcp_disabled_tools,
            ):
                if not chunk.startswith("data: "):
                    continue
                try:
                    payload = _json.loads(chunk[6:].strip())
                    if payload.get("error"):
                        error_message = str(payload.get("error") or "")
                    if payload.get("answer"):
                        answer_parts = [payload["answer"]]
                    if payload.get("turn_summary"):
                        turn_summary = payload["turn_summary"]
                    if payload.get("turn_messages"):
                        turn_messages = payload["turn_messages"]
                except Exception:
                    pass

            full = "".join(answer_parts)
            if full or turn_messages:
                if should_generate_title:
                    session_title = _generate_title_with_usage(title_seed, user_model)
                if not session_title and not existing_summary.get("title"):
                    session_title = _livecode_fallback_session_title(title_seed)
                if should_generate_title and session_title:
                    set_session_title(
                        state_path,
                        session_id,
                        session_title,
                        overwrite=True,
                    )
                title_for_new_session = session_title or None
                if turn_messages:
                    if display_payload:
                        for msg in turn_messages:
                            if msg.get("role") == "user":
                                msg["display"] = display_payload
                                break
                    append_turn_messages(
                        state_path,
                        session_id,
                        turn_messages,
                        model=user_model,
                        title=title_for_new_session,
                    )
                elif full:
                    append_turn_summary(
                        state_path,
                        session_id,
                        question_text,
                        full,
                        turn_summary=turn_summary or None,
                        model=user_model,
                        title=title_for_new_session,
                        display=display_payload,
                    )

            if error_message and not full:
                return jsonify({"success": False, "error": error_message}), 500
            return jsonify({
                "success": True,
                "answer": full,
                "turn_summary": turn_summary,
                "session_id": session_id,
                "session_title": session_title or None,
            })

        turn_lock = session_turn_lock(session_id)
        if not turn_lock.acquire(timeout=_TURN_LOCK_WAIT_S):
            return jsonify({"error": "The previous message in this chat is still finishing. Try again in a moment."}), 409
        try:
            if is_cancelled(session_id, since=requested_at):
                return jsonify({"success": True, "answer": "", "cancelled": True, "session_id": session_id})
            return _run_turn()
        finally:
            turn_lock.release()
