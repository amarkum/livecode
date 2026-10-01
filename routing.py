from __future__ import annotations

import re
from typing import Any

from livecode.session import get_projected_messages

_CODEBASE_EVIDENCE_RE = re.compile(
    r"\b(api|json|response|payload|schema|endpoint|typescript|interface|"
    r"request\s+body|workorder|work\s*order)\b",
    re.IGNORECASE,
)
_FOLLOW_UP_RE = re.compile(
    r"\b(for this|for that|this one|that one|above|those|these|the same|it)\b",
    re.IGNORECASE,
)
_STRUCTURED_JSON_RE = re.compile(
    r"(json\s+response|api\s+response|sample\s+response|response\s+structure|"
    r"proper\s+api|response\s+example|example\s+response|response\s+payload|"
    r"payload\s+(?:example|structure|schema|format))",
    re.IGNORECASE,
)
_STRUCTURE_DISCUSSION_RE = re.compile(
    r"\b(structured_output|structured\s+output|sse|event\s+stream|payload)\b",
    re.IGNORECASE,
)
_FILE_DISCOVERY_RE = re.compile(
    r"\b(where is|find file|which file|locate|list.*files|file named|files? (?:for|named|called|matching))\b",
    re.IGNORECASE,
)
_USER_WEB_REQUEST_RE = re.compile(
    r"\b(search the (?:web|internet)|look (?:it )?up online|google (?:this|for)|"
    r"search online|check online|web search|from the internet|browse (?:to|this url)|"
    r"fetch (?:this )?url|read (?:this )?url|open (?:this )?link)\b|https?://",
    re.IGNORECASE,
)
_MCP_TOOL_INTENT_RE = re.compile(
    r"\b(mcp|mcps|enabled\s+(?:mcp|tool|server)s?|disabled\s+(?:mcp|tool|server)s?|"
    r"selected\s+(?:mcp|tool|server)s?|use\s+(?:a\s+)?(?:simple\s+)?mcp|"
    r"debug(?:ger)?\s+(?:session|sessions|breakpoint|breakpoints|stack|status)|"
    r"list\s+debug\s+sessions|airflow|s3|cloudwatch|mongodb|mongo|github|gh)\b",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|thx|ok|okay|yo|cool|great|nice)(?:\s+(?:there|so much|a lot|again))?[\s!.?,:)]*$",
    re.IGNORECASE,
)
# "thanks, it works", "perfect, looks good now": the user is closing the loop, not asking for more.
_ACKNOWLEDGEMENT_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|thanks?|thank you|thx|great|perfect|awesome|nice|cool|good|sweet|brilliant|excellent|lgtm|yep|yes|got it|all good)[\s,.!]*)+"
    r"(?:(?:it|that|this|everything|the\s+\w+)\s+(?:works?|worked|looks?\s+good|fixed\s+it|did\s+it|solved\s+it|is\s+(?:fixed|working|fine|good|done|better))(?:\s+now)?|"
    r"(?:looks?|working)\s+(?:good|fine|great)(?:\s+now)?|works?(?:\s+now)?|fixed|done|all\s+good|no\s+more\s+(?:questions|changes))?[\s!.,:)]*$",
    re.IGNORECASE,
)
# A question about the code, not an instruction to change it: "why is the implementation slow?".
_QUESTION_RE = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:tell|explain|show|describe|help\s+me\s+understand)\b|"
    r"(?:why|what|how|is|are|does|do|did|where|which|who|when|whose|whom)\b)",
    re.IGNORECASE,
)
_POLITE_LEAD_RE = re.compile(r"^\s*(?:please\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?", re.IGNORECASE)
_META_RE = re.compile(
    r"\b(what can you do|your capabilities|how do you work|who are you)\b",
    re.IGNORECASE,
)
_CODE_CHANGE_RE = re.compile(
    r"\b(fix|bug|patch|implement|add test|write test|unit tests?|"
    r"exception|error handling|logging|refactor|update code|change code|bump|"
    r"rewrite|migrate|rename|convert|integrate|build (?:a|an|the|me)|create (?:a|an|the)|"
    r"add (?:a|an|the|support)|remove (?:the|all)|replace (?:the|all))\b",
    re.IGNORECASE,
)
_HARD_TASK_RE = re.compile(
    r"\b(refactor\w*|rewrit\w*|re-?architect\w*|redesign\w*|migrat\w*|overhaul\w*|"
    r"implement\w*|from scratch|end[- ]to[- ]end|"
    r"port (?:it|this|the)\b|convert (?:the|this|it|all)\b|"
    r"across (?:the )?(?:code ?base|repo|project|app|files|modules)|"
    r"(?:entire|whole) (?:code ?base|repo|project|app|module|system)|"
    r"all (?:the )?(?:files|modules|endpoints|components|call ?sites)|"
    r"every (?:file|module|component|endpoint|call ?site))",
    re.IGNORECASE,
)
_BULK_TASK_RE = re.compile(
    r"\b(across (?:the )?(?:code ?base|repo|project|app|files|modules)|(?:entire|whole) (?:code ?base|repo|project|app)|"
    r"all (?:the )?(?:files|modules|endpoints|components|call ?sites)|every (?:file|module|component|endpoint|call ?site))",
    re.IGNORECASE,
)


def is_question(question: str) -> bool:
    """A request for an explanation rather than a change ("why is X slow?", "could you explain Y?")."""
    q = (question or "").strip()
    if not q:
        return False
    if _QUESTION_RE.match(q):
        return True
    stripped = _POLITE_LEAD_RE.sub("", q)
    return bool(_QUESTION_RE.match(stripped)) and not _CODE_CHANGE_RE.match(stripped)


def is_hard_task(question: str) -> bool:
    q = question or ""
    if is_question(q) and not _BULK_TASK_RE.search(q):
        return False
    if _HARD_TASK_RE.search(q):
        return True
    return len(q) > 600 and bool(_CODE_CHANGE_RE.search(q))


def bump_for_hard_task(question: str, classification: dict[str, Any]) -> dict[str, Any]:
    if not is_hard_task(question):
        return classification
    out = dict(classification)
    out.update(
        is_meta=False,
        chat_only=False,
        is_actionable=True,
        expects_multi_step=True,
        complexity="complex",
        needs_flagship_model=True,
    )
    if str(out.get("goal_kind") or "") in ("", "meta", "analysis", "research") and _CODE_CHANGE_RE.search(question or ""):
        out["goal_kind"] = "code_change"
    if out.get("goal_kind") == "code_change":
        out["needs_code_execution"] = True
        if str(out.get("edit_scope") or "none") in ("none", "single_line", "single_file"):
            out["edit_scope"] = "multi_file"
    if _BULK_TASK_RE.search(question or ""):
        out["expects_bulk_work"] = True
        out["edit_scope"] = "bulk" if out.get("goal_kind") == "code_change" else out.get("edit_scope", "none")
    return out


# "bump the version to 2.1", "set version = '1.4.0'": an instruction with a target number, not a question
# about versions ("what version of node do I need?").
_VERSION_BUMP_RE = re.compile(
    r"\b(bump|update|change|set|increment|raise|upgrade)\b[^.?!]{0,40}\bversion\b|"
    r"\bversion\b[^.?!]{0,30}\b(?:to|=)\s*['\"]?v?\d",
    re.IGNORECASE,
)

def _intelligent_defaults(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "goal_kind": "analysis",
        "edit_scope": "none",
        "needs_flagship_model": False,
    }
    base.update(overrides)
    return base

def heuristic_classification(question: str, *, has_prior_turns: bool) -> dict[str, Any] | None:
    q = (question or "").strip()
    if not q:
        return None
    if _META_RE.search(q):
        return _intelligent_defaults(
            is_meta=True,
            is_actionable=False,
            needs_local_save=False,
            expects_bulk_work=False,
            needs_code_execution=False,
            needs_shell=False,
            chat_only=True,
            expects_multi_step=False,
            complexity="simple",
            is_follow_up=False,
            prior_context_hint="",
            goal_kind="meta",
            edit_scope="none",
            needs_flagship_model=False,
        )
    if _GREETING_RE.match(q) or _ACKNOWLEDGEMENT_RE.match(q):
        return _intelligent_defaults(
            is_meta=False,
            is_actionable=False,
            needs_local_save=False,
            expects_bulk_work=False,
            needs_code_execution=False,
            needs_shell=False,
            chat_only=True,
            expects_multi_step=False,
            complexity="simple",
            is_follow_up=False,
            prior_context_hint="",
            goal_kind="meta",
        )
    if _VERSION_BUMP_RE.search(q) and not is_question(q):
        return _intelligent_defaults(
            is_meta=False,
            is_actionable=True,
            needs_local_save=False,
            expects_bulk_work=False,
            needs_code_execution=False,
            needs_shell=False,
            chat_only=False,
            expects_multi_step=True,
            complexity="medium",
            is_follow_up=bool(has_prior_turns and _FOLLOW_UP_RE.search(q)),
            prior_context_hint="",
            goal_kind="code_change",
            edit_scope="single_line",
            needs_flagship_model=False,
        )
    if needs_codebase_evidence(q, has_prior_turns=has_prior_turns):
        return _intelligent_defaults(
            is_meta=False,
            is_actionable=True,
            needs_local_save=False,
            expects_bulk_work=False,
            needs_code_execution=False,
            needs_shell=False,
            chat_only=False,
            expects_multi_step=True,
            complexity="medium",
            is_follow_up=bool(has_prior_turns and _FOLLOW_UP_RE.search(q)),
            prior_context_hint="",
            goal_kind="research",
        )
    return None

def get_session_chat_history_for_classify(
    project_path: str,
    session_id: str,
    current_question: str,
) -> list[dict[str, Any]]:
    projected = get_projected_messages(
        project_path, session_id, current_question, wrap_query=False,
    )
    if not projected:
        return []
    last = projected[-1]
    if last.get("role") == "user" and last.get("content") == current_question:
        return projected[:-1]
    return projected

def is_acknowledgement(question: str) -> bool:
    return bool(_GREETING_RE.match(question or "") or _ACKNOWLEDGEMENT_RE.match(question or ""))


def needs_codebase_evidence(question: str, *, has_prior_turns: bool = False) -> bool:
    q = (question or "").strip()
    if not q or is_acknowledgement(q):
        return False
    if _CODEBASE_EVIDENCE_RE.search(q):
        return True
    # A follow-up ("do the same for that one") needs the code; a closing remark ("thanks, it works") was ruled out above.
    return bool(has_prior_turns and _FOLLOW_UP_RE.search(q))

def wants_structured_json(question: str) -> bool:
    q = question or ""
    if not _STRUCTURED_JSON_RE.search(q):
        return False
    if _STRUCTURE_DISCUSSION_RE.search(q) and re.search(
        r"\b(what\s+happened|why|fix\s+it|improv|debug|aborted|sudden|this)\b",
        q,
        re.IGNORECASE,
    ):
        return False
    return True

def needs_file_discovery(question: str) -> bool:
    return bool(_FILE_DISCOVERY_RE.search(question or ""))

def user_requests_web_lookup(question: str) -> bool:
    return bool(_USER_WEB_REQUEST_RE.search(question or ""))

_BROWSER_INTENT_RE = re.compile(
    r"\b(browser|screenshots?|screen shots?|web ?pages?|localhost|127\.0\.0\.1|dev server|"
    r"open (?:the |this |my )?(?:page|site|website|app|url|link)|navigate (?:to|through)|log ?in to)\b|https?://",
    re.IGNORECASE,
)

def user_requests_browser(question: str) -> bool:
    return bool(_BROWSER_INTENT_RE.search(question or "")) or browse_request(question) is not None

_UI_CHECK_RE = re.compile(
    r"\b(?:verify|check|test|confirm|look\s+at|see|show\s+me)\b[^.;!?]{0,40}\b(?:visually|in\s+the\s+(?:built[- ]in\s+)?browser|"
    r"on\s+(?:the\s+)?(?:page|screen)|how\s+it\s+looks|the\s+(?:ui|page|screen))\b|"
    r"\b(?:take\s+a\s+screenshot|screenshot\s+(?:it|the\s+(?:page|result|change))|open\s+(?:it|the\s+page)\s+in\s+the\s+browser|"
    r"in\s+the\s+(?:built[- ]in\s+)?browser)\b",
    re.IGNORECASE,
)


_NEGATED_BEFORE_RE = re.compile(
    r"\b(?:don'?t|do\s+not|dont|never|no\s+need\s+to|without|skip|not|avoid|instead\s+of|rather\s+than|wait\s+(?:for|until|before)|"
    r"before\s+you|until\s+i|hold\s+off(?:\s+on)?|hold\s+(?:it|that)|leave\s+it|unless\s+i|only\s+if\s+i)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?\n])\s+|\s+(?=\bbut\b)", re.IGNORECASE)


def _negated(sentence: str, start: int) -> bool:
    """Whether a negation or a hold-off word comes earlier in the same sentence as the match at start."""
    return bool(_NEGATED_BEFORE_RE.search(sentence[:start]))


def _sentences(text: str) -> list[tuple[int, str]]:
    """(offset, sentence) pairs: sentences split at . ; ! ? and before "but", so a negation is judged within its clause."""
    out: list[tuple[int, str]] = []
    pos = 0
    for part in _SENTENCE_SPLIT_RE.split(text or ""):
        if part is None:
            continue
        idx = text.find(part, pos)
        if idx < 0:
            idx = pos
        out.append((idx, part))
        pos = idx + len(part)
    return out


def user_requests_ui_check(question: str) -> bool:
    """The request itself asks to see the result in the browser, so checking a UI change needs no question first.
    A negated ask ("don't check the page visually") is not one."""
    if browse_request(question) is not None:
        return True
    for offset, sentence in _sentences(question or ""):
        for match in _UI_CHECK_RE.finditer(sentence):
            if not _negated(sentence, match.start()):
                return True
    return False


# A site the user names: a URL, a local dev server, or a bare domain (github.com, my-app.vercel.app).
# Bare domains ending in a TLD that is also a file extension or a word (deploy.sh, config.in, notion.so) count
# only after a strong visit verb, so "test deploy.sh" is a script and "go to notion.so" is a site.
_WEB_TLDS = r"com|org|net|io|dev|app|ai|co|uk|de|fr|es|nl|ca|au|edu|gov|xyz|info|site|tech|cloud|store|shop|page|tv|ly"
_AMBIGUOUS_TLDS = r"in|me|us|it|to|so|sh"
_SITE_PATTERN = (
    r"(?P<site>https?://[^\s<>\"']+|(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{2,5})?(?:/[^\s<>\"']*)?|"
    r"(?<![\w./-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:" + _WEB_TLDS + r")"
    r"(?![\w-])(?:/[^\s<>\"']*)?)"
)
_AMBIGUOUS_SITE_PATTERN = (
    r"(?P<site>(?<![\w./-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + _AMBIGUOUS_TLDS + r")(?![\w-])(?:/[^\s<>\"']*)?)"
)
_STRONG_VISIT_SOURCE = r"\b(?:go(?:\s+over)?\s+to|goto|visit|open(?:\s+up)?|navigate\s+to|head\s+(?:over\s+)?to|browse\s+to|take\s+me\s+to|pull\s+up|launch|surf\s+to|log\s*in\s+to|sign\s+in\s+to)\b"
_VISIT_VERB = (
    r"\b(?:go(?:\s+over)?\s+to|goto|visit|open(?:\s+up)?|navigate\s+to|head\s+(?:over\s+)?to|browse\s+(?:to\s+)?|"
    r"check(?:\s+out)?|pull\s+up|load|look\s+at|show\s+me|take\s+me\s+to|test|try|preview|launch|hit|surf\s+to|"
    r"log\s*in\s+to|sign\s+in\s+to)\b"
)
_SITE_LEAD = r"\s+(?:the\s+|this\s+|my\s+|our\s+|that\s+)?(?:(?:web)?site|web\s*page|page|website|app|url|link|dashboard|home\s*page|server)?\s*(?:at\s+|on\s+|of\s+|:\s*)?"
_VISIT_RE = re.compile(_VISIT_VERB + _SITE_LEAD + _SITE_PATTERN, re.IGNORECASE)
_STRONG_VISIT_AMBIGUOUS_RE = re.compile(_STRONG_VISIT_SOURCE + _SITE_LEAD + _AMBIGUOUS_SITE_PATTERN, re.IGNORECASE)
_ANY_SITE_RE = re.compile(_SITE_PATTERN, re.IGNORECASE)
# A URL quoted as evidence ("calling https://api.example.com/users returns 500") is not a place to go.
_URL_AS_EVIDENCE_BEFORE_RE = re.compile(
    r"\b(?:calling|called|call|GET|POST|PUT|PATCH|DELETE|fetch(?:ing|es)?|curl|wget|request(?:s|ing)?(?:\s+to)?|hitting|hit|"
    r"endpoint|returns?|response\s+from|error\s+(?:from|at|on)|failed\s+(?:on|at)|url\s*[:=]|href=|src=|base_?url)\s*[\"'`(]*$",
    re.IGNORECASE,
)
_URL_AS_EVIDENCE_AFTER_RE = re.compile(
    r"^[\"'`)]*\s*(?:returns?|returned|responds?|responded|fails?|failed|gives?|gave|throws?|threw|is\s+returning|times?\s+out|timed\s+out|"
    r"\d{3}\b|->|=>|:\s*\d{3}\b)",
    re.IGNORECASE,
)
_ERROR_REPORT_RE = re.compile(r"\b(?:traceback|stack\s*trace|exception|error|errors|failed|fails|failing|crash(?:es|ed)?|bug|[45]\d\d\b)", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`|```.*?```", re.S)


def _url_is_evidence(text: str, start: int, end: int) -> bool:
    """A URL inside a code span, or framed as what was called and what came back, is being reported, not visited."""
    for span in _CODE_SPAN_RE.finditer(text):
        if span.start() <= start and end <= span.end():
            return True
    before = text[max(0, start - 40):start]
    after = text[end:end + 40]
    if _URL_AS_EVIDENCE_BEFORE_RE.search(before) or _URL_AS_EVIDENCE_AFTER_RE.match(after):
        return True
    return False


_URL_FALLBACK_VERB_RE = re.compile(_STRONG_VISIT_SOURCE + r"|\b(?:check(?:\s+out)?|look\s+at|show\s+me|preview|screenshot|browse|browser|in\s+the\s+browser)\b", re.IGNORECASE)

# Well-known sites asked for by name, without a domain ("go to amazon and search for …").
KNOWN_SITES = {
    "google": "google.com", "youtube": "youtube.com", "amazon": "amazon.com", "github": "github.com", "gitlab": "gitlab.com",
    "gmail": "mail.google.com", "google docs": "docs.google.com", "google drive": "drive.google.com", "google maps": "maps.google.com",
    "linkedin": "linkedin.com", "twitter": "x.com", "facebook": "facebook.com", "instagram": "instagram.com", "reddit": "reddit.com",
    "wikipedia": "wikipedia.org", "stackoverflow": "stackoverflow.com", "stack overflow": "stackoverflow.com", "netflix": "netflix.com",
    "figma": "figma.com", "notion": "notion.so", "chatgpt": "chatgpt.com", "flipkart": "flipkart.com", "bing": "bing.com",
    "duckduckgo": "duckduckgo.com", "yahoo": "yahoo.com", "npm": "npmjs.com", "npmjs": "npmjs.com", "pypi": "pypi.org",
    "medium": "medium.com", "dribbble": "dribbble.com", "behance": "behance.net", "canva": "canva.com", "vercel": "vercel.com",
    "netlify": "netlify.com", "slack": "slack.com", "discord": "discord.com", "hacker news": "news.ycombinator.com",
    "product hunt": "producthunt.com", "producthunt": "producthunt.com", "myntra": "myntra.com", "swiggy": "swiggy.com",
    "zomato": "zomato.com", "ebay": "ebay.com", "walmart": "walmart.com", "etsy": "etsy.com", "airbnb": "airbnb.com",
    "booking.com": "booking.com", "mdn": "developer.mozilla.org", "codepen": "codepen.io", "dev.to": "dev.to",
}
_STRONG_VISIT = _STRONG_VISIT_SOURCE
_NAMED_SITE_RE = re.compile(
    _STRONG_VISIT + r"\s+(?:the\s+)?(?P<name>" + "|".join(sorted((re.escape(k) for k in KNOWN_SITES), key=len, reverse=True)) + r")"
    r"(?:\s+(?:website|site|web\s*site|home\s*page|page))?(?![\w.-])",
    re.IGNORECASE,
)
# The user's own running app: "check it in the browser", "open my app", "preview the site".
_APP_STRICT_RE = re.compile(r"\b(?:in|on|with|using)\s+(?:the\s+|a\s+|my\s+)?(?:built[- ]in\s+)?browser\b|"
                            r"\b(?:open|launch|preview|go\s+to|visit|navigate\s+to)\s+(?:the|my|our)\s+(?:app|site|website|web\s*app|frontend|front-end|ui|dashboard)\b", re.IGNORECASE)
_STEPS_LEAD_RE = re.compile(r"^\s*(?:[,;:.-]+\s*)?(?:and\s+(?:then\s+)?|then\s+|&\s*|,\s*)", re.IGNORECASE)


def _site_steps(text: str, end: int) -> str:
    """What the user asked to do on the site: the rest of the sentence after it ("and search for …")."""
    rest = text[end:]
    lead = _STEPS_LEAD_RE.match(rest)
    if not lead:
        return ""
    steps = re.sub(r"\s+", " ", rest[lead.end():]).strip().rstrip(".")
    return steps[:400]


def browse_request(question: str) -> dict[str, Any] | None:
    """What the user asked to open in the browser, and what to do there:
    {"site": "github.com", "steps": "check the latest issues"}, {"app": True, ...} for their own running
    app ("open my app", "check it in the browser"), or None when the message asks for neither."""
    text = question or ""
    for pattern in (_VISIT_RE, _STRONG_VISIT_AMBIGUOUS_RE):
        for match in pattern.finditer(text):
            if _url_is_evidence(text, match.start("site"), match.end("site")):
                continue
            site = match.group("site").rstrip(".,;:!?)")
            return {"site": site, "steps": _site_steps(text, match.start("site") + len(site)), "app": False}
    named = _NAMED_SITE_RE.search(text)
    if named:
        return {"site": KNOWN_SITES[named.group("name").lower()], "steps": _site_steps(text, named.end()), "app": False}
    # A URL or local address without a verb right before it still counts when the message asks to look at
    # something in the browser, unless it reads as an error report quoting the URL.
    for found in _ANY_SITE_RE.finditer(text):
        site = found.group("site")
        if not site.lower().startswith(("http://", "https://", "localhost", "127.", "0.0.0.0")):
            continue
        if _url_is_evidence(text, found.start("site"), found.end("site")):
            continue
        if not _URL_FALLBACK_VERB_RE.search(text) or _ERROR_REPORT_RE.search(text):
            continue
        site = site.rstrip(".,;:!?)")
        return {"site": site, "steps": _site_steps(text, found.start("site") + len(site)), "app": False}
    apps = list(_APP_STRICT_RE.finditer(text))
    if apps:
        steps = next((st for st in (_site_steps(text, m.end()) for m in reversed(apps)) if st), "")
        return {"site": "", "steps": steps, "app": True}
    return None


def user_requests_site_visit(question: str) -> str:
    """The site the user asked to open ("go to github.com", "open localhost:3000", "visit https://…",
    "go to amazon"), or "" when the message does not name one."""
    found = browse_request(question)
    return str(found.get("site") or "") if found else ""

# The final steps a request itself asks for: "send him a message saying …" is the go-ahead to press Send.
# Each kind needs the verb and a real target in the user's words, so that coding vocabulary ("a POST /users
# endpoint", "the email validation", "the order status", "register the route") never counts as one.
_NAME = r"(?-i:[A-Z][\w.'-]*)"  # a capitalised name even though the patterns ignore case
_PERSON = r"(?:him|her|them|me|us|" + _NAME + r")"
_OBJ = r"(?:it|this|that|them|the|a|an|my|our|your|his|her|their|this\s+\w+|these|those|one|two|three|\d+|some)"
_MESSAGE_WORDS = r"(?:message|messages|msg|email|e-mail|mail|note|reply|response|text|dm|invite|invitation|request|greeting|reminder|follow[- ]up|thank[- ]you)"
_ACTION_PATTERNS: dict[str, list[str]] = {
    "send": [
        r"\bsend(?:ing)?\s+" + _PERSON + r"\b",
        r"\bsend(?:ing)?\s+(?:" + _OBJ + r"\s+)?(?:\w+\s+){0,2}" + _MESSAGE_WORDS + r"\b",
        r"\bsend\s+(?:it|this|that|them)\b",
        r"\b(?:reply|respond)\s+(?:to\s+" + _PERSON + r"|to\s+(?:the|his|her|their|that|this)\s+(?:\w+\s+){0,2}" + _MESSAGE_WORDS + r"|with\b|saying\b)",
        r"\b(?:message|text|dm|ping|e-?mail)\s+" + _PERSON + r"\b(?!\s+(?:validation|address|field|input|format|template|regex|column|model|type|class|service|provider))",
        r"\b(?:e-?mail|dm|message)\s+(?:it|this|that)\s+to\b",
        r"\binvite\s+" + _PERSON + r"\b",
        r"\bconnect\s+with\s+" + _PERSON + r"\b",
    ],
    "post": [
        r"(?<![/@\w])post\s+(?:it|this|that|them|the\s+(?:update|post|photo|picture|image|video|comment|thread|article|story|reply|link)|a\s+(?:comment|reply|status|photo|story|thread|tweet|note)|my\s+\w+|an?\s+update)\b(?!\s*(?:request|method|route|endpoint|handler|body|data|params|hook|call|/))",
        r"\bpublish\s+(?:it|this|that|the\s+(?:post|article|story|page|release|update|draft)|my\s+\w+)\b",
        r"\btweet\b(?!\s*(?:id|url|embed|component|model))",
        r"\bcomment\s+on\s+(?:his|her|their|the|this|that|" + _NAME + r")\b",
        r"\bshare\s+(?:it|this|that|the\s+post)\s+(?:on|to|with)\b",
    ],
    "submit": [
        r"\bsubmit\s+(?:it|this|that|the|my|our|a|an)\b(?!\s*(?:button|handler|event|hook|callback|action|function|method))",
        r"\bapply\s+(?:for|to)\s+(?:the\s+|this\s+|that\s+)?(?:job|role|position|opening|vacancy|grant|visa|program|programme|internship|course|scholarship|loan|card|" + _NAME + r")",
        r"\bsign\s*up\b(?!\s*(?:page|form|flow|button|component|screen|route|endpoint|handler|modal|view))",
        r"\bregister\s+(?:for|on|at|with)\s+(?:the\s+|this\s+|that\s+)?(?:event|course|site|service|webinar|conference|meetup|newsletter|account|" + _NAME + r")",
        r"\b(?:create|open)\s+an?\s+account\s+(?:on|at|with)\b",
    ],
    "buy": [
        r"\b(?:buy|purchase)\s+" + _OBJ + r"\b(?!\s*(?:button|flow|page|form|component|screen|modal|now\s+button))",
        r"\border\s+(?:it|this|that|them|me|a|an|one|two|three|\d+|some|the\s+(?:\w+\s+)?(?:book|cable|charger|phone|laptop|item|product|parts?|food|pizza|groceries|same))\b(?!\s*(?:status|history|id|number|details|list|table|page|model|form|summary|by|of|type))",
        r"\bplace\s+(?:the|an|my|this|that)\s+order\b",
        r"\bpay\s+(?:for\s+(?:it|this|that|them|the)|the\s+(?:bill|invoice|fee|balance|amount|order)|it\s+now|now)\b",
        r"\b(?:proceed\s+to|go\s+to|complete|finish|do)\s+(?:the\s+)?check\s*out\b",
        r"\bcheck\s*out\s+(?:now|the\s+(?:cart|basket))\b",
    ],
    "book": [
        r"\b(?:book|reserve)\s+(?:me\s+)?(?:a|an|the|one|two|three|\d+|this|that|it|my|our)\b(?!\s*(?:component|page|model|form|flow|table\s+(?:schema|model|component)|list))",
    ],
    "confirm": [
        r"\bconfirm\s+(?:it|the|my|our|this|that)\s*(?:order|booking|reservation|purchase|payment|appointment|subscription|request|transfer)?\b(?!\s+(?:that|whether|if|the\s+(?:test|tests|build|fix|change|behaviou?r|output|result)))",
        r"\bconfirm\s+(?:it|the\s+(?:order|booking|reservation|purchase|payment|appointment|subscription))\b",
    ],
}
_ACTION_RES = {kind: [re.compile(p, re.IGNORECASE) for p in patterns] for kind, patterns in _ACTION_PATTERNS.items()}
# "draft it for me to review", "write it up but wait for my OK": prepare, do not send.
_DRAFT_RE = re.compile(
    r"\b(?:draft|prepare|write\s+up|compose|type\s+(?:up|out))\b[^.;!?]{0,80}\b(?:but|and)\s+(?:don'?t|do\s+not|not|wait|hold|let\s+me|check\s+with\s+me|ask\s+me)\b|"
    r"\bjust\s+(?:draft|prepare|fill|type|write)\b|\bfor\s+me\s+to\s+(?:review|check|approve|read|look\s+at|send)\b|"
    r"\bwait\s+for\s+my\s+(?:ok|okay|approval|go[- ]ahead|confirmation|review|sign[- ]off)\b|\bbefore\s+(?:you\s+)?(?:send|post|submit|buy|book)(?:ing)?\b|"
    r"\bi(?:'ll|\s+will)\s+(?:send|post|submit|review|check)\s+(?:it|them|that|this)\b|\bdon'?t\s+(?:actually\s+)?(?:send|post|submit|buy|book|order|pay)\b",
    re.IGNORECASE,
)


def authorized_final_actions(question: str) -> list[str]:
    """The kinds of final step the request itself asks for ("send", "post", "submit", "buy", "book",
    "confirm"), in the order they appear: the user asked, so doing it needs no second yes. The verb must
    come with its target in the user's words, and a negated or held-off one ("don't send it yet", "draft it
    for me to review", "wait for my OK before sending") is left out: when in doubt, nothing is authorized."""
    text = question or ""
    if _DRAFT_RE.search(text):
        return []
    kinds: list[str] = []
    for kind, patterns in _ACTION_RES.items():
        for offset, sentence in _sentences(text):
            matched = False
            for pattern in patterns:
                match = pattern.search(sentence)
                while match:
                    if not _negated(sentence, match.start()):
                        matched = True
                        break
                    match = pattern.search(sentence, match.end())
                if matched:
                    break
            if matched:
                kinds.append(kind)
                break
    return kinds


# An answer that stops to ask permission for the next step ("Shall I send it?", "Should I go ahead?").
_PERMISSION_RE = re.compile(
    r"(?:\b(?:shall|should|can|may)\s+i\b|\bdo\s+you\s+want\s+me\s+to\b|\bwould\s+you\s+like\s+me\s+to\b|\bwant\s+me\s+to\b|"
    r"\bis\s+it\s+ok(?:ay)?\s+(?:if|to)\b|\bready\s+(?:for\s+me\s+)?to\b|\bok(?:ay)?\s+to\b|\bconfirm\s+(?:that|whether|if)?\b|\blet\s+me\s+know\s+if\b)"
    r"[^?]{0,160}?\b(?P<verb>send|post|submit|publish|apply|book|buy|purchase|pay|place|proceed|go\s+ahead|continue|do\s+(?:it|that|this|so)|click|press|hit)\b[^?]{0,100}\?",
    re.I,
)


def asks_permission(answer: str) -> str:
    """The step a final answer stops to ask permission for, or "" when it does not."""
    tail = (answer or "").strip()[-500:]
    found = None
    for found in _PERMISSION_RE.finditer(tail):
        pass
    rest = tail[found.end():] if found else ""
    # Only when it ends the answer (a short "Let me know." after it is still the ending).
    if not found or "?" in rest or len(rest.strip(" \n\t*_)\"'")) > 60:
        return ""
    return re.sub(r"\s+", " ", found.group("verb").lower())


# Files whose change shows in a browser: components, pages, styles and templates.
_UI_EXTENSIONS = (".tsx", ".jsx", ".vue", ".svelte", ".astro", ".html", ".htm", ".css", ".scss", ".sass", ".less",
                  ".styl", ".pcss", ".mdx", ".hbs", ".handlebars", ".ejs", ".njk", ".jinja", ".jinja2", ".j2", ".twig",
                  ".erb", ".liquid", ".pug", ".razor", ".cshtml")
_UI_SCRIPT_EXTENSIONS = (".ts", ".js", ".mjs", ".cjs")
_UI_DIRS = re.compile(r"(^|/)(components?|pages|app|views?|screens|layouts?|ui|widgets|styles?|theme|templates|routes|"
                      r"features|containers|public|static|assets|src/mfe|microfrontends?|mfe[^/]*)/", re.IGNORECASE)
_NOT_UI = re.compile(r"(^|/)(__tests__|tests?|spec|e2e|cypress|playwright|__mocks__|stories|node_modules|dist|build)/|"
                     r"\.(test|spec|stories|story|d)\.[a-z]+$|(^|/)(vite|webpack|jest|vitest|babel|tailwind|postcss|eslint|"
                     r"prettier|next|nuxt|astro|svelte)\.config\.", re.IGNORECASE)


def is_ui_file(path: str) -> bool:
    """Whether a change to this file shows in the browser (a component, a page, a stylesheet, a template)."""
    text = str(path or "").replace("\\", "/").strip()
    low = text.lower()
    if not low or _NOT_UI.search(low):
        return False
    if low.endswith(_UI_EXTENSIONS):
        return True
    return low.endswith(_UI_SCRIPT_EXTENSIONS) and bool(_UI_DIRS.search("/" + low))


_DESIGN_INTENT_RE = re.compile(
    r"\b(?:figma|canva|sketch (?:app|file|design)|penpot|adobe xd|zeplin|framer|invision|uizard|balsamiq)\b|"
    r"\b(?:mock-?ups?|wireframes?|pixel[- ](?:perfect|by[- ]pixel))\b|"
    r"\b(?:the|this|that|my|our|a) (?:ui |visual |web |page )?designs?\b(?!\s*(?:pattern|system|token|decision|doc|document|principle|review|discussion|approach|choice|philosophy|language|guideline|goal|problem|flaw|smell|question|of\s+the\s+(?:api|schema|database|db|class|module|code)))|\bdesign (?:file|image|mock|spec|screenshot|export|link)s?\b|"
    r"\blooks? (?:exactly )?(?:like|the same as) (?:the |this |my )?(?:design|image|screenshot|mock|picture)\b|"
    r"\bmatch(?:es|ing)? (?:the |this |my )?(?:design|mock|mockup|screenshot|image)\b",
    re.IGNORECASE,
)
_UI_BUILD_RE = re.compile(
    r"\b(?:implement|build|create|make|code|recreate|replicate|clone|convert|turn|match|copy)\b.*"
    r"\b(?:ui|page|screen|layout|component|landing|site|section|header|footer|hero|button|form|card|this|it)\b",
    re.IGNORECASE,
)

def user_requests_design_work(question: str, has_images: bool = False) -> bool:
    """Building or fixing a page to look like a design (a Figma file, a mockup, an attached screenshot).
    A question about a design pattern or a design decision is not design work."""
    text = question or ""
    if _DESIGN_INTENT_RE.search(text):
        return True
    return bool(has_images and _UI_BUILD_RE.search(text))

def user_requests_mcp_or_tool_use(question: str) -> bool:
    return bool(_MCP_TOOL_INTENT_RE.search(question or ""))

def needs_code_change(question: str, classification: dict[str, Any] | None) -> bool:
    cls = classification or {}
    if str(cls.get("goal_kind") or "").lower() == "code_change":
        return True
    if cls.get("needs_code_execution") or cls.get("needs_local_save"):
        return True
    if cls.get("expects_bulk_work"):
        return True
    if cls.get("is_actionable") and not cls.get("chat_only"):
        if _CODE_CHANGE_RE.search(question or ""):
            return True
    return False

def needs_flagship_edit(classification: dict[str, Any] | None) -> bool:
    cls = classification or {}
    if cls.get("needs_flagship_model"):
        return True
    scope = str(cls.get("edit_scope") or "").lower()
    if scope in ("multi_file", "bulk"):
        return True
    if cls.get("expects_bulk_work"):
        return True
    if str(cls.get("complexity") or "").lower() == "complex" and str(cls.get("goal_kind") or "") == "code_change":
        return True
    return False

def normalize_livecode_classification(
    question: str,
    classification: dict[str, Any],
    *,
    has_prior_turns: bool,
) -> dict[str, Any]:
    out = dict(classification)
    if not needs_codebase_evidence(question, has_prior_turns=has_prior_turns) and not user_requests_mcp_or_tool_use(question):
        return out
    out["chat_only"] = False
    out["is_actionable"] = True
    out["is_meta"] = False
    if out.get("complexity") == "simple":
        out["complexity"] = "medium"
    out["expects_multi_step"] = True
    return out

def pick_tool_choice(
    iteration: int,
    classification: dict[str, Any],
    question: str,
    *,
    has_prior_turns: bool = False,
    force_required: bool = False,
) -> str:
    if force_required:
        return "required"
    if iteration != 1:
        return "auto"
    if user_requests_mcp_or_tool_use(question):
        return "required"
    if not classification.get("is_actionable"):
        return "auto"
    if classification.get("is_meta") or classification.get("chat_only"):
        return "auto"
    if (
        classification.get("needs_shell")
        or classification.get("needs_local_save")
        or classification.get("expects_bulk_work")
    ):
        return "required"
    if classification.get("expects_multi_step") or classification.get("complexity") == "complex":
        return "required"
    if needs_codebase_evidence(question, has_prior_turns=has_prior_turns):
        return "required"
    if needs_file_discovery(question):
        return "required"
    return "auto"
