"""Agent templates in a crew config export: opt-in, values redacted."""

from __future__ import annotations

import io
import json
import os
import zipfile
from unittest.mock import patch

import pytest

from kiro_crew import portability

#: Stand-ins for the values a template's MCP servers carry; obviously not real.
FAKE_ENV_VALUE = "fake-env-value-for-tests"
FAKE_HEADER_VALUE = "fake-header-value-for-tests"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A minimal install whose crews name two templates, one of which names a third."""
    mc = tmp_path / ".kirocrew"
    mc.mkdir()
    (mc / "config.json").write_text(
        json.dumps(
            {
                "agent": {"default_agent": "kirocrew"},
                "agents": {
                    "alpha": {"kiro_agent": "researcher"},
                    "beta": {"kiro_agent": "by-declared-name"},
                    "gamma": {"kiro_agent": "only-on-the-source"},
                },
            }
        ),
        encoding="utf-8",
    )
    agents = tmp_path / "kiro_agents"
    agents.mkdir()
    (agents / "researcher.json").write_text(
        json.dumps(
            {
                "name": "researcher",
                "mcpServers": {
                    "search": {
                        "command": "search-mcp",
                        "env": {"SEARCH_TOKEN": FAKE_ENV_VALUE},
                        "headers": {"Authorization": FAKE_HEADER_VALUE},
                    },
                    "odd": "not-an-object",
                },
                "toolsSettings": {
                    "subagent": {
                        "availableAgents": ["helper", "help*"],
                        "trustedAgents": ["researcher"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (agents / "helper.json").write_text(
        json.dumps(
            {
                "name": "helper",
                "mcpServers": {"web": {"url": "https://example.invalid/mcp", "headers": "flat"}},
                "toolsSettings": {"subagent": {"availableAgents": ["researcher"]}},
            }
        ),
        encoding="utf-8",
    )
    (agents / "file.json").write_text(json.dumps({"name": "by-declared-name"}), encoding="utf-8")
    (agents / "unrelated.json").write_text(json.dumps({"name": "unrelated"}), encoding="utf-8")
    (agents / "kirocrew.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    monkeypatch.setattr(portability, "kiro_agents_dir", lambda: agents)
    with patch("kiro_crew.portability.config_dir", return_value=mc):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(mc)}):
            yield mc


def _members(zip_bytes: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {info.filename.split("/", 1)[1]: zf.read(info) for info in zf.infolist()}


def _stable(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "created_at"}


class TestExportOff:
    def test_off_by_default_and_unchanged(self, home):
        default_bytes, default_manifest = portability.create_export_zip()
        off_bytes, off_manifest = portability.create_export_zip(include_agent_templates=False)
        default_members = _members(default_bytes)
        off_members = _members(off_bytes)
        assert sorted(default_members) == sorted(off_members)
        assert not any(name.startswith("agents/") for name in default_members)
        assert "agent_templates" not in default_manifest
        assert "agent_template_count" not in default_manifest["contents"]
        assert _stable(default_manifest) == _stable(off_manifest)
        for name, data in default_members.items():
            if name != "MANIFEST.json":
                assert off_members[name] == data, name

    def test_memory_only_export_carries_no_template(self, home):
        zip_bytes, manifest = portability.create_export_zip(
            memory_only=True, include_agent_templates=True
        )
        assert not any(name.startswith("agents/") for name in _members(zip_bytes))
        assert "agent_templates" not in manifest


class TestExportOn:
    def test_carries_named_templates_and_the_ones_they_name(self, home):
        zip_bytes, manifest = portability.create_export_zip(include_agent_templates=True)
        members = _members(zip_bytes)
        carried = sorted(n for n in members if n.startswith("agents/"))
        assert carried == ["agents/file.json", "agents/helper.json", "agents/researcher.json"]
        assert manifest["agent_templates"] == ["file", "helper", "researcher"]
        assert manifest["contents"]["agent_template_count"] == 3

    def test_env_and_header_values_are_placeholders(self, home):
        zip_bytes, _ = portability.create_export_zip(include_agent_templates=True)
        members = _members(zip_bytes)
        for data in members.values():
            assert FAKE_ENV_VALUE.encode() not in data
            assert FAKE_HEADER_VALUE.encode() not in data
        spec = json.loads(members["agents/researcher.json"])
        server = spec["mcpServers"]["search"]
        placeholder = portability.AGENT_TEMPLATE_SECRET_PLACEHOLDER
        assert server["env"] == {"SEARCH_TOKEN": placeholder}
        assert server["headers"] == {"Authorization": placeholder}
        assert server["command"] == "search-mcp"
        assert spec["mcpServers"]["odd"] == "not-an-object"
        helper = json.loads(members["agents/helper.json"])
        assert helper["mcpServers"]["web"]["headers"] == {}

    def test_redaction_leaves_the_source_spec_untouched(self, home, tmp_path):
        portability.create_export_zip(include_agent_templates=True)
        source = json.loads((tmp_path / "kiro_agents" / "researcher.json").read_text())
        assert source["mcpServers"]["search"]["env"] == {"SEARCH_TOKEN": FAKE_ENV_VALUE}

    def test_linked_spec_is_not_carried(self, home, tmp_path):
        agents = tmp_path / "kiro_agents"
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"name": "linked"}), encoding="utf-8")
        try:
            (agents / "linked.json").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
        cfg["agents"]["delta"] = {"kiro_agent": "linked"}
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        assert "linked" not in [stem for stem, _ in portability.bundled_agent_templates()]

    def test_walk_is_bounded(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(portability, "MAX_BUNDLED_AGENT_TEMPLATES", 1)
        assert len(portability.bundled_agent_templates()) == 1
