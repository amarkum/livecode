"""SocketIO — ide."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from livecode.workspace_config import is_livecode_workspace_file, load_livecode_workspace

IDE_EDITOR_SOFT_MAX_BYTES = 2 * 1024 * 1024


def _ide_expand(path: str) -> str:
    p = path or ""
    if p.startswith("~"):
        p = os.path.expanduser(p)
    return os.path.abspath(p)


def _ide_is_protected(path: str) -> bool:
    real = os.path.realpath(path)
    if real in ("/", os.path.expanduser("~")):
        return True
    return real.count(os.sep) < 2


def _ide_unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    n = 2
    while True:
        candidate = os.path.join(parent, f"{stem} {n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _ide_trash(path: str) -> str:
    trash_dir = os.path.join(os.path.expanduser("~"), ".Trash")
    if not os.path.isdir(trash_dir):
        trash_dir = os.path.join(os.path.expanduser("~"), ".livecode", "trash")
    os.makedirs(trash_dir, exist_ok=True)
    dest = _ide_unique_path(os.path.join(trash_dir, os.path.basename(path)))
    shutil.move(path, dest)
    return dest


def _ide_file_kind(name: str, is_dir: bool) -> str:
    if is_dir:
        if name.endswith(".app"):
            return "Application"
        return "Folder"
    lower = name.lower()
    if lower.endswith(".app"):
        return "Application"
    if lower.endswith((".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z")):
        return "ZIP archive"
    if lower.endswith((".json", ".jsonl")):
        return "JSON"
    if lower.endswith((".js", ".jsx", ".ts", ".tsx")):
        return "JavaScript"
    if lower.endswith(".py"):
        return "Python script"
    if lower.endswith((".html", ".htm")):
        return "HTML"
    if lower.endswith((".md", ".markdown")):
        return "Markdown"
    if lower.endswith((".yml", ".yaml")):
        return "YAML"
    if lower.endswith((".sh", ".bash", ".zsh")):
        return "Shell script"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return "Image"
    return "Document"


def _ide_list_directory(path: str) -> list[dict]:
    files = []
    for item in sorted(os.listdir(path), key=lambda s: s.lower()):
        item_path = os.path.join(path, item)
        try:
            is_dir = os.path.isdir(item_path)
            entry = {
                "name": item,
                "path": item_path,
                "type": "folder" if is_dir else "file",
                "is_dir": is_dir,
                "kind": _ide_file_kind(item, is_dir),
                "size": None,
                "mtime": None,
            }
            try:
                st = os.stat(item_path)
                entry["mtime"] = st.st_mtime
                if not is_dir:
                    entry["size"] = st.st_size
            except (OSError, PermissionError):
                pass
            files.append(entry)
        except (OSError, PermissionError):
            continue
    files.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"].lower()))
    return files


def register_ide_socketio(socketio, rt):
    """Register ide socketio handlers."""

    import sys as _sys
    _mod = _sys.modules[__name__]
    for _k, _v in rt.__dict__.items():
        if not _k.startswith("__"):
            setattr(_mod, _k, _v)
    g = rt.__dict__
    request = g['request']

    @socketio.on('ide_list_files')
    def on_ide_list_files(data):
        """Handle IDE file listing request."""
        requested_path = (data or {}).get('path', '~')
        try:
            path = requested_path

            if path == '~' or path.startswith('~/'):
                path = os.path.expanduser(path)

            if is_livecode_workspace_file(path) and os.path.isfile(path):
                path = load_livecode_workspace(path).primary_path

            if not os.path.exists(path):
                socketio.emit('ide_files_list', {'error': f'Path does not exist: {path}', 'path': requested_path}, room=request.sid)
                return

            if not os.path.isdir(path):
                socketio.emit('ide_files_list', {'error': f'Path is not a directory: {path}', 'path': requested_path}, room=request.sid)
                return

            files = []
            try:
                files = _ide_list_directory(path)
            except (OSError, PermissionError) as e:
                socketio.emit('ide_files_list', {'error': f'Error listing directory: {str(e)}', 'path': requested_path}, room=request.sid)
                return

            socketio.emit('ide_files_list', {'files': files, 'path': path, 'requested_path': requested_path}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_list_files: {e}")
            socketio.emit('ide_files_list', {'error': str(e), 'path': requested_path}, room=request.sid)

    @socketio.on('ide_mkdir')
    def on_ide_mkdir(data):
        """Create a folder in the IDE file browser."""
        requested_parent = (data or {}).get('path', '~')
        requested_name = ((data or {}).get('name') or '').strip()
        try:
            parent = requested_parent
            name = requested_name
            if not name or '/' in name or name in ('.', '..'):
                socketio.emit('ide_mkdir_result', {'error': 'Invalid folder name', 'parent': requested_parent, 'name': requested_name}, room=request.sid)
                return
            if parent == '~' or parent.startswith('~/'):
                parent = os.path.expanduser(parent)
            new_path = os.path.join(parent, name)
            if os.path.exists(new_path):
                socketio.emit('ide_mkdir_result', {'error': 'Folder already exists', 'parent': requested_parent, 'name': requested_name}, room=request.sid)
                return
            os.makedirs(new_path, exist_ok=False)
            socketio.emit('ide_mkdir_result', {'success': True, 'path': new_path, 'parent': parent, 'name': requested_name}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_mkdir: {e}")
            socketio.emit('ide_mkdir_result', {'error': str(e), 'parent': requested_parent, 'name': requested_name}, room=request.sid)


    @socketio.on('ide_read_file')
    def on_ide_read_file(data):
        """Handle IDE file read request."""
        requested_path = (data or {}).get('path')
        try:
            file_path = requested_path
            if not file_path:
                socketio.emit('ide_file_content', {'error': 'No path provided'}, room=request.sid)
                return

            if file_path.startswith('~'):
                file_path = os.path.expanduser(file_path)

            if not os.path.exists(file_path):
                socketio.emit('ide_file_content', {'error': f'File does not exist: {file_path}', 'path': requested_path}, room=request.sid)
                return

            if os.path.isdir(file_path):
                socketio.emit('ide_file_content', {'error': f'Path is a directory: {file_path}', 'path': requested_path}, room=request.sid)
                return

            file_size = os.path.getsize(file_path)
            large_file = file_size > IDE_EDITOR_SOFT_MAX_BYTES
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                payload = {'content': content, 'path': file_path}
                if large_file:
                    payload['large_file'] = True
                    payload['file_size'] = file_size
                socketio.emit('ide_file_content', payload, room=request.sid)
            except UnicodeDecodeError:
                with open(file_path, 'rb') as f:
                    content = f.read()
                socketio.emit('ide_file_content', {'error': 'File appears to be binary and cannot be displayed as text', 'path': requested_path}, room=request.sid)
            except Exception as e:
                socketio.emit('ide_file_content', {'error': f'Error reading file: {str(e)}', 'path': requested_path}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_read_file: {e}")
            socketio.emit('ide_file_content', {'error': str(e), 'path': requested_path}, room=request.sid)


    @socketio.on('ide_write_file')
    def on_ide_write_file(data):
        """Handle IDE file write request."""
        requested_path = (data or {}).get('path')
        try:
            file_path = requested_path
            content = data.get('content')

            if not file_path:
                socketio.emit('ide_file_saved', {'error': 'No path provided'}, room=request.sid)
                return

            if content is None:
                socketio.emit('ide_file_saved', {'error': 'No content provided', 'path': requested_path}, room=request.sid)
                return

            if file_path.startswith('~'):
                file_path = os.path.expanduser(file_path)

            if os.path.exists(file_path) and os.path.isdir(file_path):
                socketio.emit('ide_file_saved', {'error': f'Path is a directory: {file_path}', 'path': requested_path}, room=request.sid)
                return

            dir_path = os.path.dirname(file_path)
            if dir_path and not os.path.exists(dir_path):
                try:
                    os.makedirs(dir_path, exist_ok=True)
                except Exception as e:
                    socketio.emit('ide_file_saved', {'error': f'Failed to create directory: {str(e)}', 'path': requested_path}, room=request.sid)
                    return

            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                logger.info(f"IDE: Successfully saved file: {file_path}")
                socketio.emit('ide_file_saved', {'success': True, 'path': file_path}, room=request.sid)
            except Exception as e:
                logger.error(f"Error writing file {file_path}: {e}")
                socketio.emit('ide_file_saved', {'error': f'Error writing file: {str(e)}', 'path': requested_path}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_write_file: {e}")
            socketio.emit('ide_file_saved', {'error': str(e), 'path': requested_path}, room=request.sid)

    @socketio.on('ide_create_entry')
    def on_ide_create_entry(data):
        """Create an empty file or a folder inside a parent directory."""
        payload = data or {}
        parent = payload.get('parent')
        name = (payload.get('name') or '').strip()
        kind = payload.get('kind') or 'file'
        try:
            if not parent:
                socketio.emit('ide_fs_result', {'op': 'create', 'error': 'No parent provided'}, room=request.sid)
                return
            if not name or '/' in name or name in ('.', '..'):
                socketio.emit('ide_fs_result', {'op': 'create', 'error': 'Invalid name'}, room=request.sid)
                return
            parent_abs = _ide_expand(parent)
            if not os.path.isdir(parent_abs):
                socketio.emit('ide_fs_result', {'op': 'create', 'error': 'Parent is not a directory'}, room=request.sid)
                return
            target = os.path.join(parent_abs, name)
            if os.path.exists(target):
                socketio.emit('ide_fs_result', {'op': 'create', 'error': 'An item with that name already exists'}, room=request.sid)
                return
            if kind == 'folder':
                os.makedirs(target, exist_ok=False)
            else:
                with open(target, 'x', encoding='utf-8'):
                    pass
            socketio.emit('ide_fs_result', {'op': 'create', 'success': True, 'path': target, 'parent': parent_abs, 'kind': kind}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_create_entry: {e}")
            socketio.emit('ide_fs_result', {'op': 'create', 'error': str(e)}, room=request.sid)

    @socketio.on('ide_rename_entry')
    def on_ide_rename_entry(data):
        """Rename a file or folder in place."""
        payload = data or {}
        path = payload.get('path')
        new_name = (payload.get('new_name') or '').strip()
        try:
            if not path:
                socketio.emit('ide_fs_result', {'op': 'rename', 'error': 'No path provided'}, room=request.sid)
                return
            src = _ide_expand(path)
            if not os.path.exists(src):
                socketio.emit('ide_fs_result', {'op': 'rename', 'error': 'Item no longer exists'}, room=request.sid)
                return
            if _ide_is_protected(src):
                socketio.emit('ide_fs_result', {'op': 'rename', 'error': 'That location is protected'}, room=request.sid)
                return
            if not new_name or '/' in new_name or new_name in ('.', '..'):
                socketio.emit('ide_fs_result', {'op': 'rename', 'error': 'Invalid name'}, room=request.sid)
                return
            parent = os.path.dirname(src)
            dest = os.path.join(parent, new_name)
            if os.path.exists(dest):
                socketio.emit('ide_fs_result', {'op': 'rename', 'error': 'An item with that name already exists'}, room=request.sid)
                return
            os.rename(src, dest)
            socketio.emit('ide_fs_result', {'op': 'rename', 'success': True, 'old_path': src, 'path': dest, 'parent': parent}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_rename_entry: {e}")
            socketio.emit('ide_fs_result', {'op': 'rename', 'error': str(e)}, room=request.sid)

    @socketio.on('ide_delete_entry')
    def on_ide_delete_entry(data):
        """Move a file or folder to the trash."""
        payload = data or {}
        path = payload.get('path')
        try:
            if not path:
                socketio.emit('ide_fs_result', {'op': 'delete', 'error': 'No path provided'}, room=request.sid)
                return
            src = _ide_expand(path)
            if not os.path.exists(src):
                socketio.emit('ide_fs_result', {'op': 'delete', 'error': 'Item no longer exists'}, room=request.sid)
                return
            if _ide_is_protected(src):
                socketio.emit('ide_fs_result', {'op': 'delete', 'error': 'That location is protected'}, room=request.sid)
                return
            parent = os.path.dirname(src)
            trashed = _ide_trash(src)
            socketio.emit('ide_fs_result', {'op': 'delete', 'success': True, 'path': src, 'parent': parent, 'trashed_to': trashed}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_delete_entry: {e}")
            socketio.emit('ide_fs_result', {'op': 'delete', 'error': str(e)}, room=request.sid)

    @socketio.on('ide_transfer_entry')
    def on_ide_transfer_entry(data):
        """Copy or move a file or folder into a destination directory."""
        payload = data or {}
        source = payload.get('source')
        dest_dir = payload.get('dest_dir')
        mode = payload.get('mode') or 'copy'
        try:
            if not source or not dest_dir:
                socketio.emit('ide_fs_result', {'op': 'transfer', 'error': 'Missing source or destination'}, room=request.sid)
                return
            src = _ide_expand(source)
            dst_parent = _ide_expand(dest_dir)
            if not os.path.exists(src):
                socketio.emit('ide_fs_result', {'op': 'transfer', 'error': 'Source no longer exists'}, room=request.sid)
                return
            if not os.path.isdir(dst_parent):
                socketio.emit('ide_fs_result', {'op': 'transfer', 'error': 'Destination is not a directory'}, room=request.sid)
                return
            if os.path.isdir(src) and (dst_parent == src or dst_parent.startswith(src + os.sep)):
                socketio.emit('ide_fs_result', {'op': 'transfer', 'error': 'Cannot move a folder into itself'}, room=request.sid)
                return
            if mode == 'move' and _ide_is_protected(src):
                socketio.emit('ide_fs_result', {'op': 'transfer', 'error': 'That location is protected'}, room=request.sid)
                return
            target = _ide_unique_path(os.path.join(dst_parent, os.path.basename(src)))
            if mode == 'move':
                shutil.move(src, target)
            elif os.path.isdir(src):
                shutil.copytree(src, target)
            else:
                shutil.copy2(src, target)
            socketio.emit('ide_fs_result', {'op': 'transfer', 'success': True, 'mode': mode, 'source': src, 'path': target, 'dest_dir': dst_parent}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_transfer_entry: {e}")
            socketio.emit('ide_fs_result', {'op': 'transfer', 'error': str(e)}, room=request.sid)

    @socketio.on('ide_duplicate_entry')
    def on_ide_duplicate_entry(data):
        """Duplicate a file or folder next to itself."""
        payload = data or {}
        path = payload.get('path')
        try:
            if not path:
                socketio.emit('ide_fs_result', {'op': 'duplicate', 'error': 'No path provided'}, room=request.sid)
                return
            src = _ide_expand(path)
            if not os.path.exists(src):
                socketio.emit('ide_fs_result', {'op': 'duplicate', 'error': 'Item no longer exists'}, room=request.sid)
                return
            parent = os.path.dirname(src)
            stem, ext = os.path.splitext(os.path.basename(src))
            target = _ide_unique_path(os.path.join(parent, f"{stem} copy{ext}"))
            if os.path.isdir(src):
                shutil.copytree(src, target)
            else:
                shutil.copy2(src, target)
            socketio.emit('ide_fs_result', {'op': 'duplicate', 'success': True, 'source': src, 'path': target, 'parent': parent}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_duplicate_entry: {e}")
            socketio.emit('ide_fs_result', {'op': 'duplicate', 'error': str(e)}, room=request.sid)

    @socketio.on('ide_reveal_path')
    def on_ide_reveal_path(data):
        """Open the OS file manager with the given path selected."""
        payload = data or {}
        requested = payload.get('path')
        try:
            if not requested:
                socketio.emit('ide_reveal_result', {'error': 'No path provided', 'path': requested}, room=request.sid)
                return
            target = _ide_expand(requested)
            if not os.path.exists(target):
                socketio.emit('ide_reveal_result', {'error': 'Item no longer exists', 'path': requested}, room=request.sid)
                return
            if sys.platform == 'darwin':
                subprocess.run(['open', '-R', target], check=False)
            elif os.name == 'nt':
                if os.path.isdir(target):
                    subprocess.run(['explorer', target], check=False)
                else:
                    subprocess.run(['explorer', '/select,', target], check=False)
            else:
                folder = target if os.path.isdir(target) else os.path.dirname(target)
                opener = shutil.which('xdg-open') or shutil.which('gio')
                if not opener:
                    socketio.emit('ide_reveal_result', {'error': 'No file manager available', 'path': requested}, room=request.sid)
                    return
                args = [opener, 'open', folder] if opener.endswith('gio') else [opener, folder]
                subprocess.run(args, check=False)
            socketio.emit('ide_reveal_result', {'success': True, 'path': requested}, room=request.sid)
        except Exception as e:
            logger.error(f"Error in ide_reveal_path: {e}")
            socketio.emit('ide_reveal_result', {'error': str(e), 'path': requested}, room=request.sid)

