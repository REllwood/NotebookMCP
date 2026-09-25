"""Plain-text renderers.

Everything a tool returns lands in the agent's context window, so output is
terse text, not JSON: no repeated keys, no nulls, no fields the agent can't use.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .backend import NotebookRef

QUOTE_CHARS = 280
PASSAGE_CHARS = 1500

_WS = re.compile(r"\s+")


def clip(text: str, limit: int) -> str:
    text = _WS.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _status(source: Any) -> str:
    status = getattr(source, "status", None)
    name = getattr(status, "name", None)
    return name.lower() if name else ""


def notebook_list(roster: Sequence[NotebookRef], current: str | None = None) -> str:
    if not roster:
        return "No notebooks yet. Create one with `create_notebook`."
    lines = [f"{len(roster)} notebook{'s' if len(roster) != 1 else ''}:"]
    for nb in roster:
        mark = " (current)" if nb.id == current else ""
        lines.append(f"- {nb.title}{mark} · {nb.sources} sources · {nb.id}")
    return "\n".join(lines)


def source_list(notebook: NotebookRef, sources: Sequence[Any]) -> str:
    if not sources:
        return f"{notebook.title} has no sources yet. Add some with `add_source`."
    lines = [f"{notebook.title} · {len(sources)} source{'s' if len(sources) != 1 else ''}:"]
    for src in sources:
        tags = [t for t in (_label(getattr(src, "kind", "")), _status(src)) if t and t != "ready"]
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- {src.title or 'Untitled'}{tag} · {src.id}")
    return "\n".join(lines)


def answer(result: Any, titles: Mapping[str, str], *, quotes: bool = False) -> str:
    text = (getattr(result, "answer", "") or "").strip()
    if not text:
        return "NotebookLM returned no answer. The notebook may have no ready sources."
    refs = [r for r in (getattr(result, "references", None) or []) if r.source_id]
    if not refs:
        return text
    if quotes:
        cites = []
        for r in refs:
            quote = f': "{clip(r.cited_text, QUOTE_CHARS)}"' if r.cited_text else ""
            cites.append(f"[{r.citation_number}] {titles.get(r.source_id, r.source_id)}{quote}")
        return text + "\n\nCitations:\n" + "\n".join(cites)
    grouped: dict[str, list[str]] = {}
    for r in refs:
        nums = grouped.setdefault(r.source_id, [])
        if r.citation_number is not None and str(r.citation_number) not in nums:
            nums.append(str(r.citation_number))
    parts = [
        f"[{', '.join(nums)}] {titles.get(sid, sid)}" if nums else titles.get(sid, sid)
        for sid, nums in grouped.items()
    ]
    return text + "\n\nSources: " + " · ".join(parts)


def passages(
    notebook: NotebookRef, query: str, chunks: Iterable[Any], titles: Mapping[str, str]
) -> str:
    chunks = list(chunks)
    if not chunks:
        return f'No passages in "{notebook.title}" match "{query}".'
    out = [f'{len(chunks)} passage{"s" if len(chunks) != 1 else ""} from "{notebook.title}":']
    for i, chunk in enumerate(chunks, 1):
        out.append(f"\n[{i}] {titles.get(chunk.source_id, chunk.source_id)}")
        out.append(clip(chunk.text, PASSAGE_CHARS))
    return "\n".join(out)


def added(notebook: NotebookRef, source: Any, *, waited: bool) -> str:
    status = _status(source) or "processing"
    if status != "ready" and waited:
        status += " (still indexing; it will be queryable shortly)"
    kind = _label(getattr(source, "kind", ""))
    kind = f" [{kind}]" if kind and kind != "unknown" else ""
    title = source.title or "Untitled"
    return f'Added "{title}"{kind} to {notebook.title} · {status} · {source.id}'
