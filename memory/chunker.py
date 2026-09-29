from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 1600
CHUNK_OVERLAP_CHARS = 320

_HEADER_RE = re.compile(r"^(#{2,6})\s+")

@dataclass(frozen=True)
class Chunk:
    text: str
    start_line: int
    end_line: int

def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def chunk_markdown(
    content: str,
    *,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    chunk_overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    if not content:
        return []
    lines = content.splitlines()
    if not lines:
        return []

    sections = _split_by_headers(lines)
    if len(sections) <= 1 and len(content) <= max_chunk_chars:
        return [Chunk(text=content, start_line=0, end_line=len(lines))]

    chunks: list[Chunk] = []
    for section in sections:
        section_text = "\n".join(section["lines"])
        if len(section_text) <= max_chunk_chars:
            text = _add_header_context(section["header_context"], section_text)
            chunks.append(
                Chunk(
                    text=text,
                    start_line=section["start_line"],
                    end_line=section["start_line"] + len(section["lines"]),
                )
            )
        else:
            chunks.extend(
                _split_section_by_paragraphs(
                    section,
                    max_chunk_chars,
                    chunk_overlap_chars,
                )
            )
    return chunks

def _header_level(line: str) -> int | None:
    m = _HEADER_RE.match(line)
    return len(m.group(1)) if m else None

def _format_header_context(stack: list[tuple[int, str]]) -> str:
    if not stack:
        return ""
    return " > ".join(h for _, h in stack)

def _add_header_context(header_context: str, text: str) -> str:
    if not header_context:
        return text
    if text.lstrip().startswith("#"):
        return text
    return f"{header_context}\n\n{text}"

def _split_by_headers(lines: list[str]) -> list[dict]:
    sections: list[dict] = []
    current_lines: list[str] = []
    current_start = 0
    header_stack: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        level = _header_level(line)
        if level is not None:
            if current_lines:
                sections.append(
                    {
                        "lines": current_lines,
                        "start_line": current_start,
                        "header_context": _format_header_context(header_stack),
                    }
                )
                current_lines = []
            current_start = i
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()
            header_stack.append((level, line))
        current_lines.append(line)

    if current_lines:
        sections.append(
            {
                "lines": current_lines,
                "start_line": current_start,
                "header_context": _format_header_context(header_stack),
            }
        )
    return sections

def _split_section_by_paragraphs(
    section: dict,
    max_chars: int,
    overlap_chars: int,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    current_text = ""
    current_start = section["start_line"]
    line_offset = 0
    header_context = section["header_context"]
    prev_chunk_text = ""

    def flush(end_offset: int) -> None:
        nonlocal current_text, current_start, prev_chunk_text
        if not current_text.strip():
            current_text = ""
            return
        body = current_text
        if prev_chunk_text and overlap_chars > 0:
            overlap = prev_chunk_text[-overlap_chars:]
            body = f"{overlap}\n\n{body}"
        text = _add_header_context(header_context, body)
        end_line = section["start_line"] + end_offset
        chunks.append(Chunk(text=text, start_line=current_start, end_line=end_line))
        prev_chunk_text = body
        current_text = ""

    for i, line in enumerate(section["lines"]):
        is_blank = not line.strip()
        candidate = f"{current_text}\n{line}" if current_text else line
        if is_blank and current_text and len(candidate) > max_chars:
            flush(i)
            current_start = section["start_line"] + i + 1
            continue
        if len(candidate) > max_chars and current_text:
            flush(i)
            current_start = section["start_line"] + i
            if len(line) > max_chars:
                for piece in _split_oversized_line(line, max_chars):
                    text = _add_header_context(header_context, piece)
                    chunks.append(
                        Chunk(
                            text=text,
                            start_line=current_start,
                            end_line=current_start + 1,
                        )
                    )
                    prev_chunk_text = piece
                current_text = ""
                current_start = section["start_line"] + i + 1
            else:
                current_text = line
            continue
        current_text = candidate
        line_offset = i + 1

    if current_text.strip():
        flush(line_offset)
    return chunks

def _split_oversized_line(line: str, max_chars: int) -> list[str]:
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]
