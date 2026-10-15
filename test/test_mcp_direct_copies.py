"""The memory a shared server saves is MEASURED, never estimated.

``direct_copies.measure`` reads a process table; these tests hand it a fake one
under ``tmp_path`` so every number asserted is one the table holds. The rows
endpoint and the ``mcp.share`` card must carry that number through unchanged,
and carry none when there is no reading.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import direct_copies

PAGE = 4096
CMD = "/opt/mcp/chorus"


def _proc(root: Path, pid: int, ppid: int, argv: list[str], rss_pages: int) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    # A full stat line: after ``)`` come state, ppid, 19 more fields, then rss.
    fields = ["S", str(ppid), *(["0"] * 19), str(rss_pages), "0"]
    (d / "stat").write_text(f"{pid} (x y) " + " ".join(fields) + "\n", encoding="ascii")


def test_the_reading_is_the_sum_of_every_copy_and_its_children(tmp_path):
    _proc(tmp_path, 10, 1, ["kiro-cli"], 1)
    _proc(tmp_path, 11, 10, [CMD], 100)
    _proc(tmp_path, 12, 11, ["node", "child.js"], 20)  # the copy's own child
    _proc(tmp_path, 21, 1, ["kiro-cli"], 1)
    _proc(tmp_path, 22, 21, ["/usr/bin/node", CMD, "--stdio"], 180)  # interpreter + script
    _proc(tmp_path, 30, 1, [CMD.replace("chorus", "other")], 999)  # not this server

    got = direct_copies.measure({"chorus": [CMD]}, proc_root=tmp_path, page_size=PAGE)

    assert got["chorus"].copies == 2
    assert got["chorus"].rss_bytes == (100 + 20 + 180) * PAGE
    # Sharing leaves one average copy running.
    total = 300 * PAGE
    assert got["chorus"].saves_bytes == total - total // 2


def test_a_platform_without_uids_still_reads_the_table(tmp_path, monkeypatch):
    # Windows has no os.getuid; the shared process-table walker must not need it.
    _proc(tmp_path, 11, 1, [CMD], 100)
    _proc(tmp_path, 12, 1, [CMD], 60)
    monkeypatch.delattr("os.getuid", raising=False)

    got = direct_copies.measure({"chorus": [CMD]}, proc_root=tmp_path, page_size=PAGE)

    assert got["chorus"].copies == 2
    assert got["chorus"].rss_bytes == 160 * PAGE


def test_a_gateway_helper_is_not_a_per_session_copy(tmp_path):
    _proc(tmp_path, 11, 1, [CMD], 100)
    _proc(tmp_path, 12, 1, ["python", "-m", "kiro_crew.mcp_gateway.stub", CMD], 50)

    got = direct_copies.measure({"chorus": [CMD]}, proc_root=tmp_path, page_size=PAGE)

    assert got["chorus"].copies == 1


def test_one_copy_saves_nothing_and_says_no_number(tmp_path):
    _proc(tmp_path, 11, 1, [CMD], 100)

    got = direct_copies.measure({"chorus": [CMD]}, proc_root=tmp_path, page_size=PAGE)

    assert got["chorus"].saves_bytes is None


def test_no_running_copy_means_no_measurement(tmp_path):
    _proc(tmp_path, 11, 1, ["something-else"], 100)

    assert direct_copies.measure({"chorus": [CMD]}, proc_root=tmp_path, page_size=PAGE) == {}


def test_a_bare_command_name_is_never_matched(tmp_path):
    _proc(tmp_path, 11, 1, ["npx", "-y", "pkg"], 100)
    _proc(tmp_path, 12, 1, ["npx", "-y", "other"], 100)

    assert direct_copies.measure({"pkg": ["npx"]}, proc_root=tmp_path, page_size=PAGE) == {}


def test_an_unreadable_process_table_is_no_measurement(tmp_path):
    missing = tmp_path / "no-proc"

    assert direct_copies.measure({"chorus": [CMD]}, proc_root=missing, page_size=PAGE) == {}


# --- the rows endpoint carries the reading through -----------------------------


async def _rows(monkeypatch, measured):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    rows = {
        "chorus": {
            "agents": {"a"},
            "transport": "stdio",
            "entry_poolable": False,
            "env_names": set(),
            "launch_ids": set(),
            "commands": {(CMD,)},
        },
        "creds-agent": {
            "agents": {"a"},
            "transport": "stdio",
            "entry_poolable": False,
            "env_names": set(),
            "launch_ids": set(),
            "commands": {("/opt/creds",)},
        },
    }
    monkeypatch.setattr(mcp_mod, "_collect_server_rows", lambda: rows)
    monkeypatch.setattr(mcp_mod, "_load_shareability_state", lambda: ({}, {}))
    monkeypatch.setattr(mcp_mod, "_probe_currency", lambda: {})
    seen: dict = {}

    def fake_measure(commands):
        seen.update(commands)
        return measured

    monkeypatch.setattr(mcp_mod.direct_copies, "measure", fake_measure)
    resp = await mcp_mod.api_mcp_gateway_servers(SimpleNamespace(app={}, query={}))
    return {r["name"]: r for r in json.loads(resp.body)["servers"]}, seen


@pytest.mark.asyncio
async def test_rows_carry_the_measured_saving(monkeypatch):
    reading = direct_copies.DirectCopies(copies=3, rss_bytes=300_000_000)
    rows, seen = await _rows(monkeypatch, {"chorus": reading})

    assert seen["chorus"] == {(CMD,)}, "the measurement was not asked about this server's command"
    assert rows["chorus"]["saves_bytes"] == reading.saves_bytes == 200_000_000
    assert rows["chorus"]["share_blocked"] is None


@pytest.mark.asyncio
async def test_rows_carry_no_number_without_a_measurement(monkeypatch):
    rows, _seen = await _rows(monkeypatch, {})

    assert rows["chorus"]["saves_bytes"] is None
    assert rows["creds-agent"]["share_blocked"] == "credential_vending"


def test_one_binary_serving_two_servers_is_counted_per_server(tmp_path):
    """``kirocrew mcp-core`` and ``kirocrew mcp-cron`` are two servers, not one."""
    _proc(tmp_path, 11, 1, ["/opt/kirocrew", "mcp-core"], 100)
    _proc(tmp_path, 12, 1, ["/opt/kirocrew", "mcp-core"], 100)
    _proc(tmp_path, 13, 1, ["/opt/kirocrew", "mcp-cron"], 40)

    got = direct_copies.measure(
        {"core": [("/opt/kirocrew", "mcp-core")], "cron": [("/opt/kirocrew", "mcp-cron")]},
        proc_root=tmp_path,
        page_size=PAGE,
    )

    assert got["core"].copies == 2
    assert got["core"].rss_bytes == 200 * PAGE
    assert got["cron"].copies == 1
