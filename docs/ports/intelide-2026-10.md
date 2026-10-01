# IntelIDE changes to port

Source: https://codefile.io/f/mAA2kIOfBv (saved 2026-10-01). Kept verbatim below; LiveCode's mapping and status are in CHANGELOG.md.

```text
1. IntelIDE
Settings, now backed by a file
Settings is renamed and regrouped. It gains Editor, Terminal, Design and Memory tabs, and the Browser tab is reordered.
New module settings_store.py persists settings to ~/.workbench/intelide/settings.json.
Keys must match ^[A-Za-z][A-Za-z0-9_]{0,63}$, with at most 200 settings.
Values can be bool, number or string, and strings are capped at 500 characters.
Writes are atomic (temp file plus os.replace) and guarded by a lock.
New routes: GET/POST /intelide/settings and POST /intelide/settings/reset. Reset all also clears the browser preference keys (design_accuracy, match_threshold, compare_content, ui_verify, reduce_automation_signals) through the new reset_browser_settings().
The Rules list is now deduplicated by file identity (st_dev, st_ino) instead of the real path. Symlinks and hard links to the same rule file no longer show twice. This change is in both rules.py and routes.py.
Memory tab
New package intelide/memory/ with storage.py: a per-project lock, atomic writes, and a skip for ephemeral workspaces. It lets you view, edit and save MEMORY.md and session logs.
New routes: GET/POST /intelide/memory (file listing) and POST /intelide/memory/file. The file route reads when content is absent and saves when it is present.
It only accepts known editable paths (editable_memory_rel), and returns 400 for unknown paths or non-text content.
It returns 500 on OS errors, with a readable message.
New test file test_memory_editor.py.
Chat saved as the rendered transcript
Chats are now stored as the exact rendered HTML (transcript.html per session, capped at 40 MB, written atomically). Load returns transcript_html and message_count. This is the main architectural change.
New route: POST /intelide/session/transcript.
It requires project_path, session_id and html, and returns 400 on a workspace mismatch.
That mismatch check is the "only send a matching workspace" rule from the commit message.
Removed code:
save_diff_record, load_diff_records, save_tool_artifact, load_tool_artifacts, format_messages_for_display and related helpers in session.py (about 313 lines).
Their callers in harness.py.
display_subagent_result in subagent.py.
About 655 lines of test_session.py.
Frontend chat changes: sticky and collapsible long user messages, one question at a time, the command card fades out, and the scroll stays pinned while streaming.
Browser
launch_chrome_and_attach(port=9222) in browser.py:
It finds Chrome on mac, Windows or Linux, or uses INTELIDE_CHROME_EXECUTABLE.
It starts Chrome with --remote-debugging-port and a dedicated profile at ~/.workbench/intelide/chrome-debug-profile, with logs in chrome-debug.log.
It waits up to 12 s for the port, then attaches. Ports must be between 1024 and 65535, and it refuses if INTELIDE_BROWSER_CDP_URL is set.
connection_status() now reports managed_launch, pid and profile_dir.
A shared _browser_connection_update() handles launch, attach and disconnect. There is a new Socket.IO event pair, intelide_browser_connection and intelide_browser_connection_result.
Error messages now point to "Settings > Browser" instead of "Settings > Agent > Browser".
UI: theme-aware stage background, resolution and engine chips.
Prompts and tools
The hardcoded Python-quality instructions are removed from prompts.py (pre-commit, ruff, black, SonarQube guidance).
The git Co-authored-by: IntelIDE in WorkBench trailer is no longer injected into shell commands. About 40 lines are gone from tools.py, including the tool description that mentioned it.
New UI_VERIFY_CROP_GUIDE: when verifying UI, crop the shared parent container so neighbouring elements show together, and back the crop with a measurement.
UI polish
Mode chip restyled (Agent, Plan, Ask), with a chat-bubble icon for Ask.
Terminal, explorer and chat panel sizes are persisted, and divider hit areas are larger.
Terminal padding and colours.
Theme fixes: explorer backgrounds, editor line numbers, black-theme avatar, dialog backdrop, accuracy slider.
Legacy storage removed
About 120 lines are deleted from project_store.py. This covers the migration of old slug-keyed project directories and the adoption of old multi-folder workspace storage. project_dir, existing_project_dir and workspace_state_path now just use the hashed key.
Impact: old project folders that were never migrated, and old chats that exist only as message JSON, will no longer auto-migrate or rebuild for display.

2. Files / S3 (s3.py, files.js)
Multi-select in Browse S3. Checkboxes feed a unified Download action.
Batch download. /download-all now accepts a paths list.
All paths must start with s3:// and be in the same bucket, and objects are de-duplicated.
Folder entries expand by prefix.
Jobs now track saved_details (key, size, local path), total_bytes, dest_root (~/<bucket>) and selection.
/download-all-status accepts tail to trim long lists and returns bucket, module, home, start and finish times.
Folder delete. New POST /delete-s3-folder, with an in-app confirmation.
It has a dry_run mode that counts objects and returns up to 500 keys with sizes.
A real run uses batched delete_objects calls, clears the matching path-cache entries and the parent's, and reports up to 20 errors.
It reuses the existing handling for a corrupted AWS SSO token cache.
Redesigned download progress and summary modals, with folder grouping and icons.
3. Workbench shell
App catalogue spacing, avatar tint and tab styling in home.css and home.html (+689 CSS lines).
App version bump in runtime.py, plus README and TECHNICAL.md updates.
Minor cleanup in s3_browser.html (−8 lines).
Tests
Added test_settings_store.py and test_memory_editor.py, and extended test_rules.py.
Trimmed test_session.py, test_project_store.py and test_tools.py to match the removed code, and adjusted test_browser.py and test_harness.py.
Things to check
Reverse-proxy and cross-user limits. The new browser launch runs Chrome on the server host, so it only makes sense when IntelIDE runs on the user's own machine.
Transcript size. The 40 MB cap is silent: an oversize save returns success: false, not an error.
Workbench rules. This is Workbench code, which should carry no comments and no emoji. The new Python files I read have no comments. I did not check the JS and CSS.
I can go deeper into any one area, for example the intelide.js chat and transcript changes.
```
