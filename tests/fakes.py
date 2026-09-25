"""An in-memory stand-in for notebooklm-py's client, shaped like the parts the server uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

READY = SimpleNamespace(name="READY")
PROCESSING = SimpleNamespace(name="PROCESSING")


def source(id: str, title: str, kind: str = "pdf", status: Any = READY) -> SimpleNamespace:
    return SimpleNamespace(id=id, title=title, kind=kind, status=status)


def notebook(id: str, title: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(id=id, title=title, sources_count=count)


@dataclass
class Calls:
    ask: list[dict[str, Any]] = field(default_factory=list)
    search: list[dict[str, Any]] = field(default_factory=list)
    added: list[tuple[str, str, Any]] = field(default_factory=list)
    waited: list[str] = field(default_factory=list)
    list_notebooks: int = 0
    list_sources: int = 0


class FakeClient:
    def __init__(self) -> None:
        self.calls = Calls()
        self.notebook_rows = [
            notebook("nb-specs-0001", "Product Specs", 2),
            notebook("nb-research-0002", "Market Research", 1),
            notebook("nb-empty-0003", "Scratch", 0),
        ]
        self.source_rows = {
            "nb-specs-0001": [
                source("src-design", "Design Doc v2"),
                source("src-api", "API Reference", kind="web_page"),
            ],
            "nb-research-0002": [source("src-survey", "Survey 2026", kind="csv")],
            "nb-empty-0003": [],
        }
        self.notebooks = _Notebooks(self)
        self.sources = _Sources(self)
        self.chat = _Chat(self)
        self.closed = False

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


class _Notebooks:
    def __init__(self, fake: FakeClient) -> None:
        self.fake = fake

    async def list(self) -> list[Any]:
        self.fake.calls.list_notebooks += 1
        return list(self.fake.notebook_rows)

    async def create(self, title: str) -> Any:
        nb = notebook(f"nb-new-{len(self.fake.notebook_rows)}", title, 0)
        self.fake.notebook_rows.append(nb)
        self.fake.source_rows[nb.id] = []
        return nb


class _Sources:
    def __init__(self, fake: FakeClient) -> None:
        self.fake = fake

    async def list(self, notebook_id: str) -> list[Any]:
        self.fake.calls.list_sources += 1
        return list(self.fake.source_rows[notebook_id])

    async def search(self, notebook_id: str, query: str, *, limit: int | None = None) -> list[Any]:
        self.fake.calls.search.append({"notebook": notebook_id, "query": query, "limit": limit})
        return [
            SimpleNamespace(source_id="src-api", text="POST /v1/tokens   issues a token.", rank=1),
            SimpleNamespace(source_id="src-design", text="Tokens expire after 15 minutes.", rank=2),
        ][:limit]

    def _add(self, notebook_id: str, kind: str, payload: Any, title: str | None) -> Any:
        self.fake.calls.added.append((notebook_id, kind, payload))
        new_id = f"src-new-{len(self.fake.calls.added)}"
        new = source(new_id, title or str(payload), status=PROCESSING)
        self.fake.source_rows[notebook_id].append(new)
        return new

    async def add_url(self, notebook_id: str, url: str, *, title: str | None = None) -> Any:
        return self._add(notebook_id, "url", url, title or url)

    async def add_file(self, notebook_id: str, path: Path, *, title: str | None = None) -> Any:
        return self._add(notebook_id, "file", Path(path).name, title or Path(path).name)

    async def add_text(self, notebook_id: str, title: str, content: str) -> Any:
        return self._add(notebook_id, "text", content, title)

    async def wait_until_ready(self, notebook_id: str, source_id: str, timeout: float) -> Any:
        self.fake.calls.waited.append(source_id)
        row = next(s for s in self.fake.source_rows[notebook_id] if s.id == source_id)
        row.status = READY
        return row


class _Chat:
    def __init__(self, fake: FakeClient) -> None:
        self.fake = fake

    async def ask(self, notebook_id: str, question: str, source_ids: list[str]) -> Any:
        self.fake.calls.ask.append(
            {"notebook": notebook_id, "question": question, "source_ids": source_ids}
        )
        return SimpleNamespace(
            answer="Tokens last 15 minutes [1] and are issued by POST /v1/tokens [2][3].",
            references=[
                SimpleNamespace(
                    source_id="src-design",
                    citation_number=1,
                    cited_text="Tokens expire after 15 minutes.",
                ),
                SimpleNamespace(
                    source_id="src-api",
                    citation_number=2,
                    cited_text="POST /v1/tokens issues a token.",
                ),
                SimpleNamespace(source_id="src-api", citation_number=3, cited_text=None),
            ],
        )
