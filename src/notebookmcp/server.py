"""The MCP server: six small tools over one warm NotebookLM session."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, ingest
from . import format as fmt
from .backend import Backend, NoDefaultNotebook, NotebookAmbiguous, NotebookNotFound, NotebookRef

log = logging.getLogger("notebookmcp")

SOURCE_CACHE_TTL = 30.0
# Waiting for a new source to index stays under Codex's 60 s default tool timeout.
WAIT_SECONDS = 45.0

INSTRUCTIONS = """\
Answers questions from the user's Google NotebookLM notebooks. The documents stay \
in NotebookLM; only answers come back. Prefer these tools over reading large docs \
into context.
- ask: grounded answer with [n] citations. The default choice.
- search: verbatim passages, for exact wording, numbers or code.
- notebook arguments take a title, part of a title, or an id. Omit to reuse the last one."""

SIGN_IN = "Run `notebookmcp login` in a terminal, then retry."
NOT_SIGNED_IN = f"Not signed in to NotebookLM. {SIGN_IN}"
SESSION_EXPIRED = f"The NotebookLM session has expired. {SIGN_IN}"
NO_NOTEBOOKS = "You have no notebooks yet. Create one with `create_notebook`."
RATE_LIMITED = "NotebookLM is rate limiting requests. Wait a minute, then retry."
NOTEBOOK_GONE = "That notebook no longer exists. Call `list_notebooks` to refresh."

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

Notebook = Annotated[
    str, Field(description="Title, part of a title, or id. Omit to use the current notebook.")
]


def build_server(backend: Backend | None = None) -> MCPServer:
    backend = backend or Backend(default_notebook=os.environ.get("NOTEBOOKMCP_NOTEBOOK"))
    source_cache: dict[str, tuple[float, list[Any]]] = {}

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        warm = asyncio.create_task(_warm(backend))
        try:
            yield
        finally:
            warm.cancel()
            await backend.close()

    mcp = MCPServer(
        "notebooklm",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/REllwood/NotebookMCP",
        lifespan=lifespan,
    )
    # Plain-text results only: a structured copy would send every answer to the agent twice.
    tool = partial(mcp.tool, structured_output=False)

    async def notebook_sources(nb: NotebookRef, *, refresh: bool = False) -> list[Any]:
        hit = source_cache.get(nb.id)
        if hit and not refresh and time.monotonic() - hit[0] < SOURCE_CACHE_TTL:
            return hit[1]
        client = await backend.client()
        rows = await client.sources.list(nb.id)
        source_cache[nb.id] = (time.monotonic(), rows)
        return rows

    def sources_changed(nb: NotebookRef) -> None:
        source_cache.pop(nb.id, None)
        backend.forget_roster()

    @tool(annotations=READ)
    async def list_notebooks() -> str:
        """List your NotebookLM notebooks with source counts and ids."""
        async with explained(backend):
            return fmt.notebook_list(await backend.notebooks(refresh=True), backend.current)

    @tool(annotations=WRITE)
    async def ask(
        question: str,
        notebook: Notebook = "",
        sources: Annotated[
            tuple[str, ...], Field(description="Only use these sources (titles or ids).")
        ] = (),
        quotes: Annotated[bool, Field(description="Include the cited passages.")] = False,
    ) -> str:
        """Ask a notebook a question. NotebookLM answers from the notebook's sources only, \
with [n] citations. Questions to the same notebook share one chat, so follow-ups work."""
        async with explained(backend):
            nb = await backend.resolve(notebook)
            rows = await notebook_sources(nb)
            ids = pick_sources(rows, sources) if sources else [s.id for s in rows]
            if not ids:
                return f"{nb.title} has no sources yet. Add some with `add_source`."
            client = await backend.client()
            result = await client.chat.ask(nb.id, question, source_ids=ids)
            backend.remember(nb)
            return fmt.answer(result, titles(rows), quotes=quotes)

    @tool(annotations=READ)
    async def search(
        query: str,
        notebook: Notebook = "",
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
    ) -> str:
        """Return the passages from a notebook's sources that best match a query, verbatim \
and without an AI summary. Use for exact wording, numbers or code."""
        async with explained(backend):
            nb = await backend.resolve(notebook)
            rows = await notebook_sources(nb)
            client = await backend.client()
            chunks = await client.sources.search(nb.id, query, limit=limit)
            backend.remember(nb)
            return fmt.passages(nb, query, chunks, titles(rows))

    @tool(annotations=READ)
    async def list_sources(notebook: Notebook = "") -> str:
        """List the sources in a notebook."""
        async with explained(backend):
            nb = await backend.resolve(notebook)
            rows = await notebook_sources(nb, refresh=True)
            backend.remember(nb)
            return fmt.source_list(nb, rows)

    @tool(annotations=WRITE)
    async def add_source(
        url: Annotated[str, Field(description="Web page or YouTube URL.")] = "",
        path: Annotated[
            str,
            Field(
                description="Absolute path to a local file, or a folder of documents "
                "(pdf, md, txt, docx, pptx, csv, epub)."
            ),
        ] = "",
        text: Annotated[str, Field(description="Text to add as a source.")] = "",
        title: str = "",
        notebook: Notebook = "",
    ) -> str:
        """Add a URL, a local file or folder, or text to a notebook. Files upload straight \
from disk, so their contents never pass through the chat."""
        if sum(bool(v) for v in (url, path, text)) != 1:
            raise ToolError("Pass exactly one of url, path or text.")
        async with explained(backend):
            nb = await backend.resolve(notebook)
            client = await backend.client()
            if path and not Path(os.path.expanduser(path)).is_file():
                return await add_folder(client, nb, path)
            if path:
                target = ingest.plan(path).files[0]
                source = await ingest.add_path(client, nb.id, target, title=title or None)
            elif url:
                source = await client.sources.add_url(nb.id, url, title=title or None)
            else:
                source = await client.sources.add_text(nb.id, title or "Pasted text", text)
            sources_changed(nb)
            backend.remember(nb)
            source = await settle(client, nb, source)
            return fmt.added(nb, source, waited=True)

    async def add_folder(client: Any, nb: NotebookRef, path: str) -> str:
        plan = ingest.plan(path)
        results = await ingest.add_many(client, nb.id, plan.files)
        sources_changed(nb)
        backend.remember(nb)
        ok = [p.name for p, r in results if not isinstance(r, Exception)]
        failed = [f"{p.name} ({r})" for p, r in results if isinstance(r, Exception)]
        lines = [
            f"Added {len(ok)} of {len(results)} documents to {nb.title}. "
            "They index in the background, so give them a minute before asking."
        ]
        if ok:
            lines.append("Added: " + ", ".join(ok))
        if failed:
            lines.append("Failed: " + "; ".join(failed))
        if plan.skipped:
            lines.append(f"Skipped {len(plan.skipped)} non-document files.")
        return "\n".join(lines)

    @tool(annotations=WRITE)
    async def create_notebook(title: str) -> str:
        """Create an empty notebook and make it the current one."""
        async with explained(backend):
            client = await backend.client()
            created = await client.notebooks.create(title)
            nb = NotebookRef(id=created.id, title=created.title or title, sources=0)
            backend.forget_roster()
            backend.remember(nb)
            return f'Created "{nb.title}" · {nb.id}. It is now the current notebook.'

    return mcp


def titles(rows: list[Any]) -> dict[str, str]:
    return {s.id: s.title or "Untitled" for s in rows}


def pick_sources(rows: list[Any], wanted: tuple[str, ...]) -> list[str]:
    picked: list[str] = []
    for ref in wanted:
        needle = ref.strip().casefold()
        hits = [s for s in rows if s.id == ref.strip()]
        hits = hits or [s for s in rows if (s.title or "").casefold() == needle]
        hits = hits or [s for s in rows if needle and needle in (s.title or "").casefold()]
        if len(hits) != 1:
            names = ", ".join(s.title or s.id for s in rows) or "none"
            problem = "matches several sources" if hits else "matches no source"
            raise ToolError(f'"{ref}" {problem}. Sources: {names}')
        if hits[0].id not in picked:
            picked.append(hits[0].id)
    return picked


async def settle(client: Any, nb: NotebookRef, source: Any) -> Any:
    """Give a new source a chance to finish indexing so the next question can use it."""
    try:
        return await client.sources.wait_until_ready(nb.id, source.id, timeout=WAIT_SECONDS)
    except Exception as exc:
        if _is(exc, "SourceTimeoutError"):
            return source
        if _is(exc, "SourceProcessingError"):
            raise ToolError(
                f'NotebookLM could not process "{source.title or source.id}". '
                "It may be private, paywalled or an unsupported format."
            ) from exc
        raise


async def _warm(backend: Backend) -> None:
    """Import the client library off the event loop and open the session before the first call."""
    try:
        await asyncio.to_thread(importlib.import_module, "notebooklm")
        await backend.notebooks()
    except Exception:
        log.debug("warm-up skipped", exc_info=True)


def _is(exc: BaseException, name: str) -> bool:
    lib = sys.modules.get("notebooklm.exceptions")
    kind = getattr(lib, name, None) if lib else None
    return kind is not None and isinstance(exc, kind)


@asynccontextmanager
async def explained(backend: Backend) -> AsyncIterator[None]:
    """Turn failures into one-line messages the agent can act on."""
    try:
        yield
    except ToolError:
        raise
    except NoDefaultNotebook as exc:
        roster: list[NotebookRef] = exc.args[0]
        if not roster:
            raise ToolError(NO_NOTEBOOKS) from exc
        raise ToolError(
            "Which notebook? Pass `notebook`. You have: " + ", ".join(nb.title for nb in roster)
        ) from exc
    except NotebookNotFound as exc:
        known = ", ".join(nb.title for nb in backend.cached()) or "none"
        raise ToolError(f'No notebook matches "{exc.args[0]}". You have: {known}') from exc
    except NotebookAmbiguous as exc:
        ref, matches = exc.args
        options = "; ".join(f"{nb.title} ({nb.id})" for nb in matches)
        raise ToolError(f'"{ref}" matches several notebooks: {options}. Pass the id.') from exc
    except ingest.IngestError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        if "login" in str(exc):
            raise ToolError(NOT_SIGNED_IN) from exc
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        if _is(exc, "AuthError"):
            await backend.close()
            raise ToolError(SESSION_EXPIRED) from exc
        if _is(exc, "RateLimitError"):
            raise ToolError(RATE_LIMITED) from exc
        if _is(exc, "NotebookNotFoundError"):
            backend.forget_roster()
            raise ToolError(NOTEBOOK_GONE) from exc
        log.exception("tool call failed")
        raise ToolError(f"NotebookLM request failed: {type(exc).__name__}: {exc}") from exc
