from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from notebookmcp import cli


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    async def ok() -> int:
        return 0

    ran: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        ran.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "status", ok)
    monkeypatch.setattr(cli, "launch_command", lambda: ["/opt/bin/notebookmcp"])
    monkeypatch.setattr(subprocess, "run", fake_run)
    return ran


def test_setup_registers_with_every_installed_agent(
    signed_in: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cli.run_setup("all", "Product Specs") == 0
    assert signed_in == [
        ["claude", "mcp", "add", "--scope", "user", "notebooklm",
         "-e", "NOTEBOOKMCP_NOTEBOOK=Product Specs", "--", "/opt/bin/notebookmcp"],
        ["codex", "mcp", "add", "notebooklm",
         "--env", "NOTEBOOKMCP_NOTEBOOK=Product Specs", "--", "/opt/bin/notebookmcp"],
    ]  # fmt: skip


def test_setup_skips_missing_agents(
    signed_in: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None
    )
    assert cli.run_setup("all", None) == 0
    assert signed_in == [["codex", "mcp", "add", "notebooklm", "--", "/opt/bin/notebookmcp"]]


def test_setup_with_no_agents_prints_manual_steps(
    signed_in: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.run_setup("all", None) == 1
    assert (
        "claude mcp add --scope user notebooklm -- /opt/bin/notebookmcp" in capsys.readouterr().out
    )


def test_login_defaults_to_browser_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(subprocess, "call", lambda cmd: seen.append(cmd) or 0)
    assert cli.main(["login"]) == 0
    assert seen[0][-3:] == ["notebooklm", "login", "--browser-cookies"]
    cli.main(["login", "--browser", "chrome"])
    assert seen[1][-3:] == ["login", "--browser", "chrome"]
