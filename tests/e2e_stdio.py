#!/usr/bin/env python
"""
End-to-end check of the MCP server over a real stdio connection.

Unlike the unit tests, which call the plain functions directly, this
spawns `python -m django purge_mcp_server` as a subprocess and speaks
the JSON-RPC wire protocol to it, the same way an MCP client would.
Run: python tests/e2e_stdio.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


async def send(proc: asyncio.subprocess.Process, message: dict) -> None:
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()


async def recv(proc: asyncio.subprocess.Process) -> dict:
    line = await proc.stdout.readline()
    if not line:
        fail("server closed stdout unexpectedly")
    return json.loads(line)


async def call_tool(proc: asyncio.subprocess.Process, request_id: int, name: str, arguments: dict) -> dict:
    await send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    response = await recv(proc)
    if "result" not in response:
        fail(f"{name} call failed outright: {response}")
    return response["result"]


async def run_checks(db_path: str) -> None:
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "tests.settings"
    env["DBPURGE_TEST_DB"] = db_path
    env["PYTHONPATH"] = str(REPO_ROOT)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "django",
        "purge_mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(REPO_ROOT),
        env=env,
    )

    try:
        await send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e_stdio", "version": "0"},
                },
            },
        )
        init_response = await recv(proc)
        if "result" not in init_response:
            fail(f"initialize failed: {init_response}")
        await send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        preview = await call_tool(
            proc,
            2,
            "preview_purge",
            {
                "app_name": "tests",
                "model_name": "samplerecord",
                "time_column": "created_at",
                "retention_seconds": 3600,
            },
        )
        if preview.get("isError"):
            fail(f"preview_purge returned an error for a valid policy: {preview}")
        candidates = preview["structuredContent"]
        if candidates["row_count"] != 1:
            fail(f"expected row_count 1 (only the stale row), got {candidates}")
        sample_rows = candidates["sample_rows"]
        if len(sample_rows) != 1 or set(sample_rows[0]) != {"pk", "created_at"}:
            fail(f"unexpected sample_rows shape: {sample_rows}")
        if not candidates.get("confirmation_token") or not candidates.get("token_expires_at"):
            fail(f"missing token fields: {candidates}")

        bad_model = await call_tool(
            proc,
            3,
            "preview_purge",
            {
                "app_name": "tests",
                "model_name": "nope",
                "time_column": "created_at",
                "retention_seconds": 3600,
            },
        )
        if not bad_model.get("isError"):
            fail(f"expected an error for an unknown model, got {bad_model}")

        bad_column = await call_tool(
            proc,
            4,
            "preview_purge",
            {
                "app_name": "tests",
                "model_name": "samplerecord",
                "time_column": "label",
                "retention_seconds": 3600,
            },
        )
        if not bad_column.get("isError"):
            fail(f"expected an error for a non-date column, got {bad_column}")

        execute_args = {
            "app_name": "tests",
            "model_name": "samplerecord",
            "time_column": "created_at",
            "retention_seconds": 3600,
            "confirmation_token": candidates["confirmation_token"],
        }
        executed = await call_tool(proc, 5, "execute_purge", execute_args)
        if executed.get("isError"):
            fail(f"execute_purge returned an error for a valid token: {executed}")
        result = executed["structuredContent"]
        if result["deleted_count"] != 1:
            fail(f"expected deleted_count 1, got {result}")

        reused = await call_tool(proc, 6, "execute_purge", execute_args)
        if not reused.get("isError"):
            fail(f"expected a reused (already-consumed) token to be rejected, got {reused}")

        after_delete = await call_tool(
            proc,
            7,
            "preview_purge",
            {
                "app_name": "tests",
                "model_name": "samplerecord",
                "time_column": "created_at",
                "retention_seconds": 3600,
            },
        )
        if after_delete["structuredContent"]["row_count"] != 0:
            fail(f"expected the stale row to be gone, got {after_delete}")
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()


def seed_database() -> None:
    from django.core.management import call_command
    from django.db import connections
    from django.utils import timezone

    call_command("migrate", run_syncdb=True, verbosity=0)

    from tests.models import SampleRecord

    SampleRecord.objects.create(created_at=timezone.now() - timedelta(days=10), label="stale")
    SampleRecord.objects.create(created_at=timezone.now(), label="fresh")

    # Release the sqlite connection before the subprocess opens the
    # same file, to avoid a spurious "database is locked" error.
    connections.close_all()


def main() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
    os.environ["DBPURGE_TEST_DB"] = db_path
    try:
        import django

        django.setup()
        seed_database()
        asyncio.run(run_checks(db_path))
        print("OK: stdio e2e checks passed")
    finally:
        os.remove(db_path)


if __name__ == "__main__":
    main()
