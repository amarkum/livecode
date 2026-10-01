from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

MAX_FRAMES = 6

SNAPSHOT_JS = r"""
(opts) => {
  opts = opts || {};
  const max = opts.max || 250;
  const viewOnly = !!opts.viewOnly;
  const query = String(opts.query || "").trim().toLowerCase();
  if (window.__livecodeRefN == null) window.__livecodeRefN = opts.refBase || 0;
  const isMain = !opts.frame;
  const txt = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  const vw = innerWidth, vh = innerHeight;

  const deepAll = (start, sel, cap) => {
    const out = [];
    let scanned = 0;
    const walk = (node) => {
      if (!node.querySelectorAll) return;
      for (const el of node.querySelectorAll(sel)) out.push(el);
      for (const el of node.querySelectorAll("*")) {
        if (++scanned > (cap || 30000)) return;
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(start);
    return out;
  };

  const root = opts.selector ? document.querySelector(opts.selector) : document;
  if (!root) return { error: "Nothing on the page matches the selector " + opts.selector + "." };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && Number(s.opacity) !== 0;
  };

  const nameOf = (el) => {
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute("type") || "").toLowerCase();
    let n = txt(el.getAttribute("aria-label"));
    if (!n) {
      const ids = el.getAttribute("aria-labelledby");
      if (ids) {
        const scope = el.getRootNode();
        n = txt(ids.split(/\s+/).map((id) => { const t = (scope.getElementById ? scope.getElementById(id) : null) || document.getElementById(id); return t ? t.textContent : ""; }).join(" "));
      }
    }
    if (!n && (tag === "input" || tag === "textarea" || tag === "select")) {
      if (el.labels && el.labels.length) n = txt(Array.from(el.labels).map((l) => l.textContent).join(" "));
      if (!n) { const wrap = el.closest("label"); if (wrap) n = txt(wrap.textContent); }
      if (!n) n = txt(el.getAttribute("placeholder"));
      if (!n && (type === "submit" || type === "button" || type === "reset")) n = txt(el.value);
    }
    if (!n && tag !== "input" && tag !== "textarea") n = txt(el.innerText);
    if (!n) {
      const img = el.querySelector ? el.querySelector("img[alt], svg title") : null;
      n = txt(el.getAttribute("title")) || txt(el.getAttribute("alt")) || (img ? txt(img.getAttribute && img.getAttribute("alt") || img.textContent) : "") || txt(el.getAttribute("name")) || txt(el.id);
    }
    n = n.replace(/\s*\.?\s*Press (return|enter) to[^.]*\.?$/i, "").replace(/\b(.{3,60}?)\s+\1\b/g, "$1").trim();
    return n.slice(0, 100);
  };

  const selector = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], [role=link], ' +
    '[role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=switch], [role=textbox], ' +
    '[role=combobox], [role=searchbox], [role=slider], [contenteditable=""], [contenteditable=true], [onclick], [tabindex="0"]';

  const dialogEls = [];
  if (isMain) {
    for (const el of deepAll(document, 'dialog[open], [role=dialog], [role=alertdialog], [aria-modal=true]', 12000)) if (visible(el)) dialogEls.push(el);
    if (!dialogEls.length && document.body) {
      const overlays = [];
      for (const top of document.body.children) {
        overlays.push(top);
        for (const child of top.children) overlays.push(child);
      }
      for (const el of overlays.slice(0, 400)) {
        const s = getComputedStyle(el);
        if ((s.position !== "fixed" && s.position !== "absolute") || (Number(s.zIndex) || 0) < 10 || !visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width * r.height < vw * vh * 0.12) continue;
        if (!el.querySelector("button, a[href], input, [role=button]")) continue;
        if (txt(el.innerText).length > 1500) continue;
        dialogEls.push(el);
      }
    }
  }
  const inDialog = (el) => dialogEls.some((d) => d.contains(el) || (el.getRootNode && el.getRootNode().host && d.contains(el.getRootNode().host)));

  const candidates = deepAll(root, selector, 30000);
  let items = [];
  let index = 0;
  for (const el of candidates) {
    if (index > 4000) break;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const pos = r.bottom <= 0 ? -1 : r.top >= vh ? 1 : 0;
    const horiz = r.right <= 0 || r.left >= vw;
    if (viewOnly && (pos !== 0 || horiz)) continue;
    const name = nameOf(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const href = tag === "a" ? (el.getAttribute("href") || "").slice(0, 200) : "";
    if (query && !(name + " " + href + " " + (el.value || "")).toLowerCase().includes(query)) continue;
    let role = el.getAttribute("role");
    if (!role) {
      if (tag === "a") role = "link";
      else if (tag === "button" || tag === "summary") role = "button";
      else if (tag === "select") role = "select";
      else if (tag === "textarea") role = "textbox";
      else if (tag === "input") role = ["checkbox", "radio", "submit", "button", "reset", "file", "range", "search"].includes(type) ? type : "textbox";
      else role = el.isContentEditable ? "textbox" : "clickable";
    }
    items.push({ el: el, order: index++, pos: pos, dist: pos === 0 ? 0 : Math.min(Math.abs(r.top - vh), Math.abs(r.bottom)), role: role, name: name, href: href, tag: tag, type: type, dialog: inDialog(el), y: Math.round(r.top + scrollY) });
  }
  const totalFound = items.length;
  if (items.length > max) {
    const rank = items.slice().sort((a, b) => (b.dialog - a.dialog) || (a.pos !== 0) - (b.pos !== 0) || a.dist - b.dist || a.order - b.order);
    items = rank.slice(0, max).sort((a, b) => a.order - b.order);
  }
  const elements = items.map((it) => {
    const existing = Number(it.el.getAttribute("data-livecode-ref"));
    const ref = existing > 0 ? existing : ++window.__livecodeRefN;
    if (!existing) it.el.setAttribute("data-livecode-ref", String(ref));
    const el = it.el;
    const item = { ref: ref, role: it.role, name: it.name };
    if (it.href) item.href = it.href;
    if (it.tag === "input" || it.tag === "textarea") {
      if (it.type && it.type !== "text") item.type = it.type;
      if (it.type !== "password" && el.value) item.value = String(el.value).slice(0, 80);
      if (el.getAttribute("placeholder") && el.getAttribute("placeholder") !== it.name) item.placeholder = el.getAttribute("placeholder").slice(0, 80);
      if (el.required) item.required = true;
    }
    if (it.tag === "select" && el.selectedOptions && el.selectedOptions[0]) item.value = el.selectedOptions[0].text.slice(0, 80);
    if (el.checked) item.checked = true;
    if (el.disabled) item.disabled = true;
    if (el.getAttribute("aria-expanded")) item.expanded = el.getAttribute("aria-expanded") === "true";
    if (el.getAttribute("aria-selected") === "true" || el.getAttribute("aria-current") === "page") item.selected = true;
    if (it.pos !== 0) item.pos = it.pos;
    if (it.dialog) item.dialog = true;
    item._el = el;
    return item;
  });
  const seenNames = {};
  elements.forEach((e) => { const k = e.role + "|" + e.name; seenNames[k] = (seenNames[k] || 0) + 1; });
  elements.forEach((e) => {
    if (seenNames[e.role + "|" + e.name] < 2 || e.name.length > 30) return;
    let node = e._el.parentElement;
    for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
      const head = node.querySelector("h1, h2, h3, h4, h5, h6, [role=heading], strong, b");
      const own = txt(head ? head.textContent : "");
      if (own && own !== e.name) { e.context = own.slice(0, 60); return; }
      const t = txt(node.innerText).replace(e.name, "").trim();
      if (t.length >= 8 && node.children.length > 1) { e.context = t.slice(0, 60); return; }
    }
  });

  let text = "";
  const scopeEl = root === document ? document.body : root;
  if (viewOnly && scopeEl) {
    const parts = [], walker = document.createTreeWalker(scopeEl, NodeFilter.SHOW_TEXT), range = document.createRange();
    let total = 0;
    while (walker.nextNode() && total < 6000) {
      const node = walker.currentNode, t = txt(node.nodeValue);
      if (!t || !node.parentElement || !visible(node.parentElement)) continue;
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width > 0 && r.bottom > 0 && r.top < vh) { parts.push(t); total += t.length + 1; }
    }
    text = parts.join("\n");
  } else if (scopeEl) {
    text = scopeEl.innerText || "";
  }
  text = text.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  let matches = 0;
  if (query) {
    const lines = text.split("\n");
    const keep = new Set();
    lines.forEach((line, i) => { if (line.toLowerCase().includes(query)) { matches++; for (let k = Math.max(0, i - 1); k <= Math.min(lines.length - 1, i + 1); k++) keep.add(k); } });
    text = Array.from(keep).sort((a, b) => a - b).map((i) => lines[i]).join("\n");
  }

  const maxY = Math.max(0, Math.round(Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0) - innerHeight));
  const out = { title: document.title, url: location.href, text: text, elements: elements.map((e) => { const c = Object.assign({}, e); delete c._el; return c; }),
    more: Math.max(0, totalFound - elements.length), viewOnly: viewOnly, scroll: [Math.round(scrollY), maxY], query: query, matches: matches };
  if (!isMain || viewOnly && !opts.overview) return out;

  const refOf = (el) => { const hit = el.hasAttribute && el.hasAttribute("data-livecode-ref") ? el : (el.querySelector ? el.querySelector("[data-livecode-ref]") : null); return hit ? Number(hit.getAttribute("data-livecode-ref")) : 0; };
  const refItems = (container, limit) => elements.filter((e) => container.contains(e._el)).slice(0, limit).map((e) => ({ ref: e.ref, name: e.name, role: e.role }));

  const overview = {};
  overview.headings = Array.from(document.querySelectorAll("h1, h2")).filter(visible).slice(0, 6).map((h) => ({ level: h.tagName.toLowerCase(), text: txt(h.textContent).slice(0, 90) })).filter((h) => h.text);

  overview.dialogs = dialogEls.slice(0, 2).map((d) => {
    const heading = d.querySelector("h1, h2, h3, [role=heading]");
    return { text: txt(heading ? heading.textContent : d.innerText).slice(0, 140), buttons: refItems(d, 8) };
  });

  const bodyText = txt((document.title || "") + " " + (document.body ? document.body.innerText.slice(0, 2500) : ""));
  const challengeFrame = Array.from(document.querySelectorAll("iframe")).some((f) => {
    const r = f.getBoundingClientRect();
    return /recaptcha|hcaptcha|turnstile|captcha|challenge/i.test((f.src || "") + " " + (f.title || "")) && r.width >= 200 && r.height >= 100 && r.bottom > 0 && r.top < vh && getComputedStyle(f).visibility !== "hidden";
  });
  const shortPage = bodyText.length < 2500;
  let blocked = null;
  if (challengeFrame || (shortPage && /verify (that )?you('| a)re (a )?human|are you a robot|complete the captcha|prove you.re (not )?a (robot|human)|security check/i.test(bodyText))) blocked = { kind: "captcha", reason: "The page asks to prove you are human." };
  else if (bodyText.length < 1800 && /access denied|request blocked|unusual traffic|just a moment|checking your browser|attention required|pardon our interruption|temporarily blocked|forbidden|too many requests|error 403|error 429|403 forbidden/i.test(bodyText)) blocked = { kind: "blocked", reason: "The site refused or is challenging the browser." };
  overview.blocked = blocked;

  overview.forms = Array.from(document.forms).filter(visible).slice(0, 4).map((f) => {
    const fields = elements.filter((e) => e._el.form === f || f.contains(e._el)).filter((e) => !["submit", "button", "reset"].includes(e.role));
    const submit = elements.find((e) => (e._el.form === f || f.contains(e._el)) && (e.role === "submit" || (e._el.tagName === "BUTTON" && e._el.type === "submit") || (e.role === "button" && /submit|search|sign|log|continue|next|buy|pay|book|save|send/i.test(e.name))));
    return { name: txt(f.getAttribute("aria-label") || f.getAttribute("name") || f.id || "").slice(0, 40), fields: fields.slice(0, 8).map((e) => ({ ref: e.ref, name: e.name, role: e.role, required: !!e.required })), submit: submit ? { ref: submit.ref, name: submit.name } : null };
  }).filter((f) => f.fields.length);

  const repeated = [];
  const containers = deepAll(document, "main, section, ul, ol, div, tbody, nav", 6000).slice(0, 2500);
  for (const c of containers) {
    const kids = c.children;
    if (kids.length < 4 || kids.length > 300) continue;
    const sig = (k) => k.tagName + "." + Array.from(k.classList).slice(0, 2).join(".");
    const counts = {};
    for (const k of kids) counts[sig(k)] = (counts[sig(k)] || 0) + 1;
    let best = "", n = 0;
    for (const key in counts) if (counts[key] > n) { best = key; n = counts[key]; }
    if (n < 4) continue;
    const members = Array.from(kids).filter((k) => sig(k) === best && visible(k));
    const texts = members.map((k) => txt(k.innerText)).filter((t) => t.length >= 12);
    if (texts.length < 4) continue;
    if (repeated.some((r) => r.el.contains(c) || c.contains(r.el))) continue;
    const avg = texts.reduce((a, t) => a + t.length, 0) / texts.length;
    repeated.push({ el: c, count: members.length, score: members.length * Math.sqrt(avg), sample: members.slice(0, 3).map((k) => ({ text: txt(k.innerText).slice(0, 100), ref: refOf(k) })), where: c.tagName.toLowerCase() + (c.id ? "#" + c.id : "") });
  }
  repeated.sort((a, b) => b.score - a.score);
  overview.repeated = repeated.slice(0, 3).map((r) => ({ count: r.count, sample: r.sample, where: r.where }));

  const hasPassword = !!document.querySelector("input[type=password]");
  const prices = (bodyText.match(/[₹$€£]\s?\d[\d,.]*/g) || []).length;
  const buy = elements.some((e) => /add to (cart|bag|basket)|buy now|book now|book tickets|select seats|checkout/i.test(e.name));
  let type = "page";
  if (blocked) type = "blocked";
  else if (/\b(404|page not found|not found)\b/i.test(document.title) || (bodyText.length < 400 && /\b404\b|page not found/i.test(bodyText))) type = "error";
  else if (hasPassword && overview.forms.length) type = "login";
  else if (buy && prices) type = "product or booking";
  else if (overview.repeated.length && overview.repeated[0].count >= 5) type = "listing or results";
  else if (overview.forms.some((f) => f.fields.length >= 3)) type = "form";
  else if (document.querySelector("article") && text.length > 2500) type = "article";
  overview.type = type;
  overview.counts = { links: elements.filter((e) => e.role === "link").length, buttons: elements.filter((e) => e.role === "button").length, fields: elements.filter((e) => ["textbox", "search", "select", "checkbox", "radio"].includes(e.role)).length };
  overview.landmarks = ["header", "nav", "main", "aside", "footer"].filter((t) => document.querySelector(t) || document.querySelector("[role=" + (t === "header" ? "banner" : t === "footer" ? "contentinfo" : t === "aside" ? "complementary" : t === "nav" ? "navigation" : "main") + "]"));
  overview.frames = document.querySelectorAll("iframe").length;
  out.overview = overview;
  return out;
}
"""

MARKS_JS = r"""
(limit) => {
  const old = document.getElementById("__livecode_marks");
  if (old) old.remove();
  const box = document.createElement("div");
  box.id = "__livecode_marks";
  box.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
  let n = 0;
  const seen = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll("[data-livecode-ref]")) {
      if (n >= limit) return;
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4 || r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) continue;
      if (seen.some((s) => Math.abs(s.left - r.left) < 3 && Math.abs(s.top - r.top) < 3 && Math.abs(s.width - r.width) < 3)) continue;
      seen.push(r);
      const frame = document.createElement("div");
      frame.style.cssText = "position:absolute;box-sizing:border-box;border:1.5px solid #ff2d8c;left:" + r.left + "px;top:" + r.top + "px;width:" + r.width + "px;height:" + r.height + "px;";
      const tag = document.createElement("span");
      tag.textContent = el.getAttribute("data-livecode-ref");
      tag.style.cssText = "position:absolute;left:-1.5px;top:" + (r.top < 14 ? "auto" : "-14px") + ";bottom:" + (r.top < 14 ? "-14px" : "auto") + ";background:#ff2d8c;color:#fff;font:600 10px/13px -apple-system,Segoe UI,sans-serif;padding:0 3px;border-radius:2px;";
      frame.appendChild(tag);
      box.appendChild(frame);
      n++;
    }
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  document.documentElement.appendChild(box);
  return n;
}
"""

UNMARK_JS = "() => { const b = document.getElementById('__livecode_marks'); if (b) b.remove(); }"


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url[:40]
    except ValueError:
        return url[:40]


def collect(refs: dict[str, dict[int, Any]], page: Any, *, max_elements: int, view_only: bool = False, query: str = "",
            selector: str = "") -> dict[str, Any]:
    opts = {"max": max_elements, "viewOnly": view_only, "query": query, "selector": selector, "frame": 0, "overview": True}
    data = page.evaluate(SNAPSHOT_JS, opts)
    if data.get("error"):
        return data
    for item in data.get("elements") or []:
        refs["desc"][item["ref"]] = {"role": item.get("role"), "name": item.get("name"), "href": item.get("href"), "frame": 0}
    data["frames"] = []
    if selector:
        return data
    used = len(data.get("elements") or [])
    frames = [f for f in page.frames if f != page.main_frame and (f.url or "") not in ("", "about:blank")][:MAX_FRAMES]
    for index, frame in enumerate(frames, start=1):
        room = max_elements - used
        if room <= 0:
            break
        try:
            box = frame.frame_element().bounding_box()
            if box is None or box["width"] < 20 or box["height"] < 20:
                continue
            sub = frame.evaluate(SNAPSHOT_JS, {"max": room, "viewOnly": view_only, "query": query, "refBase": index * 10000, "frame": index})
        except Exception:
            continue
        elements = sub.get("elements") or []
        if not elements:
            continue
        for item in elements:
            refs["frames"][item["ref"]] = frame
            refs["desc"][item["ref"]] = {"role": item.get("role"), "name": item.get("name"), "href": item.get("href"), "frame": index}
        used += len(elements)
        data["frames"].append({"host": _host(frame.url), "elements": elements, "text": (sub.get("text") or "")[:600]})
    return data


def _elem_line(item: dict[str, Any]) -> str:
    bits = [f"[{item.get('ref')}] {item.get('role') or 'element'}"]
    if item.get("name"):
        bits.append(json.dumps(item["name"], ensure_ascii=False))
    for key in ("type", "placeholder", "value", "href"):
        if item.get(key):
            bits.append(f"{key}={json.dumps(item[key], ensure_ascii=False)}")
    for key in ("checked", "disabled", "required", "selected"):
        if item.get(key):
            bits.append(key)
    if "expanded" in item:
        bits.append("expanded" if item["expanded"] else "collapsed")
    if item.get("context"):
        bits.append(f"(in: {json.dumps(item['context'], ensure_ascii=False)})")
    if item.get("dialog"):
        bits.append("in dialog")
    if item.get("pos") == 1:
        bits.append("(below the view)")
    elif item.get("pos") == -1:
        bits.append("(above the view)")
    return " ".join(bits)


def _overview_lines(over: dict[str, Any]) -> list[str]:
    lines = ["Overview:"]
    if over.get("type"):
        counts = over.get("counts") or {}
        detail = f"{counts.get('links', 0)} links, {counts.get('buttons', 0)} buttons, {counts.get('fields', 0)} fields listed"
        if over.get("frames"):
            detail += f", {over['frames']} iframe" + ("s" if over["frames"] != 1 else "")
        lines.append(f"- Looks like: {over['type']} ({detail})")
    blocked = over.get("blocked")
    if blocked:
        lines.append(f"- BLOCKED ({blocked.get('kind')}): {blocked.get('reason')} Scripts and scrolling will not get past this: "
                     "tell the user, or ask them to attach their own Chrome (Settings > Browser) or import their cookies.")
    if over.get("type") == "login":
        lines.append("- This is a sign-in page: the browser is not signed in to this site. Never type credentials. Ask the user to sign in in the "
                     "Browser tab (they can take control), or to import cookies from their browser / attach their Chrome in Settings, then continue.")
    for dialog in over.get("dialogs") or []:
        buttons = ", ".join(f"[{b['ref']}] {b['name'] or b['role']}" for b in dialog.get("buttons") or [])
        lines.append(f"- A dialog or overlay is open and probably blocks the page: \"{dialog.get('text')}\"" + (f". Its controls: {buttons}" if buttons else ""))
    for form in over.get("forms") or []:
        fields = ", ".join(f"[{f['ref']}] {f['name'] or f['role']}" + (" *" if f.get("required") else "") for f in form.get("fields") or [])
        submit = form.get("submit")
        label = f" \"{form['name']}\"" if form.get("name") else ""
        lines.append(f"- Form{label}: {fields}" + (f" -> submit [{submit['ref']}] {submit['name']}" if submit else ""))
    for group in over.get("repeated") or []:
        samples = "; ".join((f"[{s['ref']}] " if s.get("ref") else "") + json.dumps(s["text"], ensure_ascii=False) for s in group.get("sample") or [])
        lines.append(f"- {group.get('count')} similar items in <{group.get('where')}>, e.g. {samples}")
    heads = over.get("headings") or []
    if heads:
        lines.append("- Headings: " + " | ".join(f"{h['level']} {h['text']}" for h in heads))
    landmarks = over.get("landmarks") or []
    if landmarks:
        lines.append("- Regions: " + ", ".join(landmarks))
    return lines


def format_snapshot(data: dict[str, Any], max_chars: int) -> str:
    lines = [f"Page: {data.get('title') or '(untitled)'}", f"URL: {data.get('url') or ''}"]
    if data.get("waited"):
        lines.append(f"Waited {data['waited']}s for the page to render (it was empty at first, like an app still starting up).")
    if data.get("viewOnly"):
        scroll = data.get("scroll") or [0, 0]
        where = f" (scrolled {scroll[0]} of {scroll[1]} px)" if scroll[1] > 0 else ""
        lines.append(f"In view now{where}. Only what is on screen is listed.")
    if data.get("query"):
        lines.append(f"Filtered to {json.dumps(data['query'])}: {data.get('matches', 0)} matching text lines.")
    over = data.get("overview")
    if over and data.get("viewOnly"):
        over = {"blocked": over.get("blocked"), "dialogs": over.get("dialogs")} if (over.get("blocked") or over.get("dialogs")) else None
    if over:
        lines.append("")
        lines.extend(_overview_lines(over))
    elements = data.get("elements") or []
    if elements:
        lines.append("")
        lines.append("Interactive elements (pass ref to click or type):")
        lines.extend(_elem_line(item) for item in elements)
    elif data.get("query"):
        lines.append("No interactive element matches.")
    if data.get("more"):
        lines.append(f"… {data['more']} more interactive elements are not listed"
                     + ("" if data.get("viewOnly") else ": snapshot again with query or selector to narrow it, or read the page with javascript_exec"))
    for frame in data.get("frames") or []:
        lines.append("")
        lines.append(f"Inside an iframe from {frame.get('host')}:")
        lines.extend(_elem_line(item) for item in frame.get("elements") or [])
        if frame.get("text"):
            lines.append("  Text: " + frame["text"].replace("\n", " ")[:300])
    head = "\n".join(lines)
    text = str(data.get("text") or "")
    room = max(1000, max_chars - len(head))
    if len(text) > room:
        text = text[:room].rstrip() + (f"\n… [{len(text) - room} more characters of page text not shown; scrolling does not reveal them: "
                                        "snapshot again with query (a word to find) or selector (one part of the page), or read it with javascript_exec]")
    return head + "\n\nText:\n" + (text or "(no visible text)")
