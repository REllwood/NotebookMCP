from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from notebookmcp.backend import Backend
from notebookmcp.server import NOT_SIGNED_IN, build_server

from .fakes import FakeClient, notebook


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
async def client(fake: FakeClient):
    backend = Backend(client_factory=lambda: fake)
    async with Client(build_server(backend)) as connected:
        yield connected


async def call(client: Client, tool: str, **args: object) -> tuple[str, bool]:
    result = await client.call_tool(tool, args)
    return "\n".join(block.text for block in result.content), bool(result.is_error)


async def test_toolset_is_small_and_text_only(client: Client) -> None:
    tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == [
        "list_notebooks",
        "ask",
        "search",
        "list_sources",
        "add_source",
        "create_notebook",
    ]
    assert all(t.description and t.output_schema is None for t in tools)


async def test_list_notebooks(client: Client) -> None:
    text, error = await call(client, "list_notebooks")
    assert not error
    assert text.splitlines()[0] == "3 notebooks:"
    assert "- Product Specs · 2 sources · nb-specs-0001" in text


async def test_ask_by_partial_title_groups_citations(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "ask", question="How long do tokens last?", notebook="specs")
    assert not error
    assert text.startswith("Tokens last 15 minutes [1]")
    assert text.endswith("Sources: [1] Design Doc v2 · [2, 3] API Reference")
    assert fake.calls.ask[0]["notebook"] == "nb-specs-0001"
    assert fake.calls.ask[0]["source_ids"] == ["src-design", "src-api"]


async def test_ask_with_quotes(client: Client) -> None:
    text, _ = await call(client, "ask", question="q", notebook="Product Specs", quotes=True)
    assert '[1] Design Doc v2: "Tokens expire after 15 minutes."' in text
    assert "[3] API Reference" in text


async def test_follow_up_reuses_the_notebook_and_cached_sources(
    client: Client, fake: FakeClient
) -> None:
    await call(client, "ask", question="first", notebook="research")
    _, error = await call(client, "ask", question="follow-up")
    assert not error
    assert [c["notebook"] for c in fake.calls.ask] == ["nb-research-0002"] * 2
    assert fake.calls.list_sources == 1


async def test_ask_without_notebook_asks_which(client: Client) -> None:
    text, error = await call(client, "ask", question="anything")
    assert error
    assert "Which notebook?" in text and "Market Research" in text


async def test_default_notebook_from_config(fake: FakeClient) -> None:
    backend = Backend(default_notebook="Market Research", client_factory=lambda: fake)
    async with Client(build_server(backend)) as client:
        _, error = await call(client, "ask", question="anything")
    assert not error
    assert fake.calls.ask[0]["notebook"] == "nb-research-0002"


async def test_unknown_and_ambiguous_notebooks(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "ask", question="q", notebook="nope")
    assert error and 'No notebook matches "nope"' in text and "Scratch" in text

    fake.notebook_rows.append(notebook("nb-specs-9999", "Product Specs (old)", 0))
    await call(client, "list_notebooks")
    text, error = await call(client, "list_sources", notebook="nb-specs")
    assert error and "matches several notebooks" in text


async def test_ask_restricted_to_named_sources(client: Client, fake: FakeClient) -> None:
    _, error = await call(client, "ask", question="q", notebook="specs", sources=["api ref"])
    assert not error
    assert fake.calls.ask[0]["source_ids"] == ["src-api"]

    text, error = await call(client, "ask", question="q", notebook="specs", sources=["missing"])
    assert error and '"missing" matches no source' in text


async def test_ask_on_empty_notebook(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "ask", question="q", notebook="Scratch")
    assert not error and "no sources yet" in text
    assert not fake.calls.ask


async def test_search_returns_verbatim_passages(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "search", query="token expiry", notebook="specs", limit=2)
    assert not error
    assert text.splitlines()[0] == '2 passages from "Product Specs":'
    assert "[1] API Reference\nPOST /v1/tokens issues a token." in text
    assert fake.calls.search[0]["limit"] == 2


async def test_list_sources_marks_non_ready_and_type(client: Client) -> None:
    text, _ = await call(client, "list_sources", notebook="specs")
    assert "- Design Doc v2 [pdf] · src-design" in text
    assert "- API Reference [web_page] · src-api" in text


async def test_add_source_needs_exactly_one_input(client: Client) -> None:
    text, error = await call(client, "add_source", notebook="specs")
    assert error and "exactly one of url, path or text" in text
    text, error = await call(client, "add_source", notebook="specs", url="https://x", text="y")
    assert error


async def test_add_url_waits_until_ready(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "add_source", notebook="specs", url="https://example.com")
    assert not error
    assert fake.calls.added == [("nb-specs-0001", "url", "https://example.com")]
    assert fake.calls.waited == ["src-new-1"]
    assert "· ready ·" in text


async def test_new_source_is_used_by_the_next_question(client: Client, fake: FakeClient) -> None:
    await call(client, "ask", question="q", notebook="specs")
    await call(client, "add_source", text="Tokens can be revoked.", title="Revocation")
    await call(client, "ask", question="q")
    assert fake.calls.ask[-1]["source_ids"][-1] == "src-new-1"


async def test_add_local_files(client: Client, fake: FakeClient, tmp_path: Path) -> None:
    (tmp_path / "guide.md").write_text("# Guide")
    (tmp_path / "notes.py").write_text("print('hi')")
    _, error = await call(client, "add_source", notebook="specs", path=str(tmp_path / "guide.md"))
    assert not error and fake.calls.added[-1][1:] == ("file", "guide.md")

    # A named text file that isn't a document type is sent as pasted text.
    await call(client, "add_source", notebook="specs", path=str(tmp_path / "notes.py"))
    assert fake.calls.added[-1][1:] == ("text", "print('hi')")


async def test_add_folder(client: Client, fake: FakeClient, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "sub" / "c.py").write_text("x = 1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "d.md").write_text("d")
    text, error = await call(client, "add_source", notebook="specs", path=str(tmp_path))
    assert not error
    assert sorted(entry[2] for entry in fake.calls.added) == ["a.md", "b.pdf"]
    assert "Added 2 of 2 documents" in text and "Skipped 1 non-document files." in text


async def test_add_missing_path(client: Client) -> None:
    text, error = await call(client, "add_source", notebook="specs", path="/no/such/file.pdf")
    assert error and "No such file or directory" in text


async def test_create_notebook_becomes_current(client: Client, fake: FakeClient) -> None:
    text, error = await call(client, "create_notebook", title="Onboarding")
    assert not error and 'Created "Onboarding"' in text
    await call(client, "add_source", url="https://example.com/handbook")
    assert fake.calls.added[-1][0] == "nb-new-3"


async def test_not_signed_in_message() -> None:
    def factory() -> FakeClient:
        raise FileNotFoundError("Storage file not found: x\nRun 'notebooklm login' first.")

    async with Client(build_server(Backend(client_factory=factory))) as client:
        text, error = await call(client, "list_notebooks")
    assert error and text.endswith(NOT_SIGNED_IN)
