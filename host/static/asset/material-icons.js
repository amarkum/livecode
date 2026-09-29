// File and folder icons from Material Icon Theme (MIT):
// https://github.com/material-extensions/vscode-material-icon-theme
// Resolution follows VS Code: exact file/folder name, then longest extension, then default.
(function () {
  var BASE = "/asset/material-icons/";
  var map = null;

  try {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", BASE + "icon-map.json", false);
    xhr.send(null);
    if (xhr.status === 200) map = JSON.parse(xhr.responseText);
  } catch (e) {}

  function url(icon) {
    return BASE + icon + ".svg";
  }

  function baseName(path) {
    return String(path || "").split(/[\\/]/).filter(Boolean).pop() || "";
  }

  window.getFileIcon = function (name) {
    var file = baseName(name).toLowerCase();
    if (!map) return url("file");
    if (map.fileNames[file]) return url(map.fileNames[file]);
    var parts = file.split(".");
    for (var i = 1; i < parts.length; i++) {
      var ext = parts.slice(i).join(".");
      if (map.fileExtensions[ext]) return url(map.fileExtensions[ext]);
    }
    return url(map.file);
  };

  window.getFolderIcon = function (name, expanded) {
    var folder = baseName(name).toLowerCase();
    if (!map) return url(expanded ? "folder-open" : "folder");
    var named = expanded ? map.folderNamesExpanded[folder] : map.folderNames[folder];
    if (named) return url(named);
    return url(expanded ? map.folderExpanded : map.folder);
  };
})();
