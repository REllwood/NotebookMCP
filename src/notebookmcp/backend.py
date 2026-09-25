"""One long-lived NotebookLM session shared by every tool call.

Opening a NotebookLM client costs a round trip (cookie load + CSRF fetch), so the
server opens it once, keeps it warm, and caches the notebook roster briefly so
name-based lookups don't cost an extra request per call.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Protocol

NOTEBOOK_CACHE_TTL = 60.0
KEEPALIVE_SECONDS = 900.0


class NotebookNotFound(LookupError):
    """No notebook matches the reference the caller gave."""


class NotebookAmbiguous(LookupError):
    """More than one notebook matches the reference the caller gave."""


class NoDefaultNotebook(LookupError):
    """The caller gave no notebook and there is nothing sensible to fall back to."""


@dataclass(frozen=True)
class NotebookRef:
    id: str
    title: str
    sources: int


class ClientFactory(Protocol):
    def __call__(self) -> Any: ...


def _default_factory() -> Any:
    from notebooklm import NotebookLMClient

    return NotebookLMClient.from_storage(
        profile=os.environ.get("NOTEBOOKMCP_PROFILE") or None,
        keepalive=KEEPALIVE_SECONDS,
    )


class Backend:
    """Owns the NotebookLM client, notebook resolution and the session's default notebook."""

    def __init__(
        self,
        *,
        default_notebook: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._default_ref = default_notebook
        self._factory = client_factory or _default_factory
        self._client: Any = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        self._roster: list[NotebookRef] = []
        self._roster_at = 0.0
        self._last_used: str | None = None

    async def client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                stack = AsyncExitStack()
                try:
                    self._client = await stack.enter_async_context(self._factory())
                except BaseException:
                    await stack.aclose()
                    raise
                self._stack = stack
        return self._client

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    async def notebooks(self, *, refresh: bool = False) -> list[NotebookRef]:
        fresh = time.monotonic() - self._roster_at < NOTEBOOK_CACHE_TTL
        if refresh or not fresh or not self._roster:
            client = await self.client()
            rows = await client.notebooks.list()
            self._roster = [
                NotebookRef(id=nb.id, title=nb.title or "Untitled", sources=nb.sources_count or 0)
                for nb in rows
            ]
            self._roster_at = time.monotonic()
        return self._roster

    def cached(self) -> list[NotebookRef]:
        return list(self._roster)

    def forget_roster(self) -> None:
        self._roster_at = 0.0

    @property
    def current(self) -> str | None:
        """Id of the notebook this session used last."""
        return self._last_used

    def remember(self, notebook: NotebookRef) -> None:
        self._last_used = notebook.id

    async def resolve(self, ref: str | None) -> NotebookRef:
        """Turn an id, id prefix or title into a notebook.

        With no reference: the configured default, then the notebook used last in
        this session, then the account's only notebook.
        """
        ref = (ref or "").strip() or None
        if ref is None:
            ref = self._default_ref or self._last_used
        roster = await self.notebooks()
        if ref is None:
            if len(roster) == 1:
                return roster[0]
            raise NoDefaultNotebook(roster)
        found = _match(roster, ref)
        if found is None:
            # A notebook created in the web UI a moment ago won't be in the cache yet.
            found = _match(await self.notebooks(refresh=True), ref)
        if found is None:
            raise NotebookNotFound(ref)
        return found


def _match(roster: list[NotebookRef], ref: str) -> NotebookRef | None:
    needle = ref.casefold()
    for nb in roster:
        if nb.id == ref:
            return nb
    for bucket in (
        [nb for nb in roster if nb.title.casefold() == needle],
        [nb for nb in roster if len(ref) >= 6 and nb.id.startswith(ref)],
        [nb for nb in roster if needle in nb.title.casefold()],
    ):
        if len(bucket) == 1:
            return bucket[0]
        if len(bucket) > 1:
            raise NotebookAmbiguous(ref, bucket)
    return None
