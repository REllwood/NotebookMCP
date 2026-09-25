"""`notebookmcp` command line: run the server, sign in, check status, register with agents."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys

from . import __version__

SERVER_NAME = "notebooklm"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # `login` hands everything after it to the NotebookLM sign-in command untouched.
    if argv[:1] == ["login"]:
        return login(argv[1:])

    parser = argparse.ArgumentParser(
        prog="notebookmcp",
        description="NotebookLM for Claude Code and Codex, over MCP. "
        "With no command, runs the MCP server on stdio.",
    )
    parser.add_argument("--version", action="version", version=f"notebookmcp {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.add_parser("serve", help="run the MCP server on stdio (the default)")
    commands.add_parser("login", help="sign in to NotebookLM using your browser's session")
    commands.add_parser("status", help="check sign-in and list your notebooks")
    setup = commands.add_parser("setup", help="sign in and register with Claude Code and Codex")
    setup.add_argument(
        "--client",
        choices=["claude", "codex", "all"],
        default="all",
        help="which agent to register with (default: every one installed)",
    )
    setup.add_argument("--notebook", help="default notebook title or id for this machine")
    args = parser.parse_args(argv)

    if args.command == "status":
        return asyncio.run(status())
    if args.command == "setup":
        return run_setup(args.client, args.notebook)
    return serve()


def serve() -> int:
    from .server import build_server

    build_server().run("stdio")
    return 0


def login(extra: list[str]) -> int:
    """Delegate to notebooklm-py's sign-in. By default it copies the session from your browser."""
    if not extra:
        print("Reading your NotebookLM session from your browser.")
        print("Sign in at https://notebooklm.google.com first if you haven't.")
        print("Your OS may ask to allow access to the browser's cookie store.\n")
        extra = ["--browser-cookies"]
    code = subprocess.call([sys.executable, "-m", "notebooklm", "login", *extra])
    if code == 0:
        print("\nSigned in. Check it with: notebookmcp status")
    else:
        print(
            "\nSign-in didn't complete. Other ways in:\n"
            "  notebookmcp login --browser-cookies chrome   pick the browser to read from\n"
            "  notebookmcp login --account you@gmail.com --browser-cookies\n"
            "  notebookmcp login --browser chrome           sign in through a browser window "
            "(needs the [browser] extra)"
        )
    return code


async def status() -> int:
    from mcp.server.mcpserver.exceptions import ToolError

    from .backend import Backend
    from .format import notebook_list
    from .server import explained

    backend = Backend()
    try:
        async with explained(backend):
            roster = await backend.notebooks()
    except ToolError as exc:
        print(exc)
        return 1
    finally:
        await backend.close()
    print("Signed in to NotebookLM.\n" + notebook_list(roster))
    return 0


def launch_command() -> list[str]:
    installed = shutil.which("notebookmcp")
    return [installed] if installed else [sys.executable, "-m", "notebookmcp"]


def run_setup(client: str, notebook: str | None) -> int:
    signed_in = asyncio.run(status()) == 0
    if not signed_in and (login([]) != 0 or asyncio.run(status()) != 0):
        return 1

    command = launch_command()
    env = [f"NOTEBOOKMCP_NOTEBOOK={notebook}"] if notebook else []
    targets = ["claude", "codex"] if client == "all" else [client]
    registered = 0
    print()
    for agent in targets:
        if not shutil.which(agent):
            if client != "all":
                print(f"{agent} isn't on your PATH; install it first.")
            continue
        if agent == "claude":
            flags = [arg for pair in env for arg in ("-e", pair)]
            cmd = ["claude", "mcp", "add", "--scope", "user", SERVER_NAME, *flags, "--", *command]
        else:
            flags = [arg for pair in env for arg in ("--env", pair)]
            cmd = ["codex", "mcp", "add", SERVER_NAME, *flags, "--", *command]
        done = subprocess.run(cmd, capture_output=True, text=True)
        output = (done.stdout + done.stderr).strip()
        if done.returncode == 0:
            print(f"✓ Registered with {agent}.")
            registered += 1
        elif "already exists" in output:
            print(f"✓ Already registered with {agent}.")
            registered += 1
        else:
            print(f"✗ Couldn't register with {agent}: {output or done.returncode}")

    if not registered:
        print("No agent registered. Add this MCP server by hand:")
        print(f"  claude mcp add --scope user {SERVER_NAME} -- {' '.join(command)}")
        print(f"  codex mcp add {SERVER_NAME} -- {' '.join(command)}")
        return 1
    print("\nDone. Restart your agent and ask it something like:")
    print('  "Ask my NotebookLM notebook what the onboarding doc says about deploys."')
    return 0
