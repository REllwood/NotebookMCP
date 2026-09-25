"""Get local files into a notebook without routing their bytes through the chat."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# What NotebookLM's upload endpoint accepts as a file.
UPLOADABLE = frozenset({".pdf", ".md", ".markdown", ".txt", ".docx", ".pptx", ".csv", ".epub"})
# Other document formats that are plain text: sent as a pasted-text source instead.
TEXT_DOCS = frozenset({".rst", ".adoc", ".asciidoc", ".org", ".tex"})
SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}
)
MAX_FILES = 50
MAX_TEXT_BYTES = 2_000_000
UPLOAD_CONCURRENCY = 4


class IngestError(ValueError):
    """The path can't be added as it stands; the message says why."""


@dataclass
class Plan:
    files: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)


def plan(raw: str) -> Plan:
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.exists():
        raise IngestError(f"No such file or directory: {path}")
    if path.is_file():
        if not _addable(path, directory=False):
            raise IngestError(
                f"{path.name}: NotebookLM can't read this file type. "
                f"Use {', '.join(sorted(UPLOADABLE))} or any UTF-8 text file."
            )
        return Plan(files=[path])

    found = Plan()
    for root, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(names):
            if name.startswith("."):
                continue
            file = Path(root, name)
            (found.files if _addable(file, directory=True) else found.skipped).append(file)
    if not found.files:
        raise IngestError(f"No documents found under {path}.")
    if len(found.files) > MAX_FILES:
        raise IngestError(
            f"{len(found.files)} documents under {path}; the limit per call is {MAX_FILES}. "
            "Point at a narrower folder."
        )
    return found


def _addable(path: Path, *, directory: bool) -> bool:
    ext = path.suffix.lower()
    if ext in UPLOADABLE or ext in TEXT_DOCS:
        return True
    # A single named file gets the benefit of the doubt if it reads as text;
    # a folder walk sticks to document formats so source code isn't swept up.
    return not directory and _is_text(path)


def _is_text(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return False
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return True


async def add_path(client: Any, notebook_id: str, path: Path, *, title: str | None = None) -> Any:
    if path.suffix.lower() in UPLOADABLE:
        return await client.sources.add_file(notebook_id, path, title=title)
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise IngestError(f"{path.name} is over {MAX_TEXT_BYTES // 1_000_000} MB of text.")
    text = path.read_text(encoding="utf-8")
    return await client.sources.add_text(notebook_id, title or path.name, text)


async def add_many(client: Any, notebook_id: str, files: list[Path]) -> list[tuple[Path, Any]]:
    """Upload files concurrently. Each result is the new source or the exception it raised."""
    gate = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def one(path: Path) -> tuple[Path, Any]:
        async with gate:
            try:
                return path, await add_path(client, notebook_id, path)
            except Exception as exc:  # reported per file, never aborts the batch
                return path, exc

    return list(await asyncio.gather(*(one(f) for f in files)))
