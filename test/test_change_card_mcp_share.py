"""The ``mcp.share`` change card: one press shares a server, through the switch's path."""

from __future__ import annotations

import hashlib

import pytest

from kiro_crew import change_card_catalog as catalog

_EXPECTED = "a" * 64 + ":" + "b" * 64


def _test_state():
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig

    serving = frozenset(KiroCrewConfig.load().mcp_gateway.stub_servers) | {"slack-mcp"}
    return SimpleNamespace(_mcp_gateway_serving=lambda: serving)


def _digest8(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _snap(**over):
    base = {
        "exists": True,
        "can_share": True,
        "shared": False,
        "share_blocked": None,
        "launch_commands": ["/opt/slack --stdio"],
        "launch_env_lines": ["SLACK_TOKEN=<hidden sha256:00>"],
        "expected_launch": _EXPECTED,
        "sharing_on": True,
        "other_marked": [],
    }
    base.update(over)
    return base


def _preview(params, snapshot):
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, params)
    return catalog.build_preview(catalog.KIND_MCP_SHARE, p, snapshot, {})


def test_the_kind_is_listed_for_the_guide():
    kinds = {k["id"]: k for k in catalog.list_kinds()}
    assert "mcp.share" in kinds
    assert set(kinds["mcp.share"]["params_schema"]["required"]) == {"server", "shared"}


def test_one_press_goes_through_the_same_endpoint_the_switch_uses():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert preview["apply"] == [
        {
            "method": "POST",
            "path": "/api/mcp-gateway/servers/stub",
            "body": {"name": "slack-mcp", "stub": True, "expected_launch": _EXPECTED},
        }
    ]
    assert ("POST", "/api/mcp-gateway/servers/stub") in catalog.HOOKED_ROUTES
    assert preview["changes"][0] == {
        "field": "server",
        "label": "slack-mcp",
        "before": "One per chat",
        "after": "Shared",
    }


_CHORUS = {
    "name": "chorus-mcp",
    "launches": [
        {"command": "/opt/chorus  --stdio", "env": ["LOG_LEVEL=warn", "TOKEN=<hidden sha256:11>"]}
    ],
}
_GRASP = {
    "name": "grasp-mcp",
    "launches": [
        {"command": "/opt/grasp --profile work", "env": []},
        {"command": "/opt/grasp --profile home", "env": ["LD_PRELOAD=/tmp/x.so"]},
    ],
}


def test_sharing_with_the_switch_off_turns_it_on_and_shows_what_else_starts():
    """Turning sharing on starts the other marked servers: each one's launch is on the card."""
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(
            sharing_on=False,
            other_marked=["chorus-mcp", "grasp-mcp"],
            other_launches=[_CHORUS, _GRASP],
        ),
    )

    assert [s["path"] for s in preview["apply"]] == [
        "/api/mcp-gateway/servers/stub",
        "/api/mcp-gateway/enable",
    ]
    assert preview["apply"][1]["body"] == {"enabled": True}
    assert ("POST", "/api/mcp-gateway/enable") in catalog.HOOKED_ROUTES
    # The title names every server the press shares, not only the one asked for.
    assert preview["title"] == "Share slack-mcp, chorus-mcp, grasp-mcp"
    rows = preview["changes"]
    assert {"label": "Sharing", "before": "Off", "after": "On"} in rows
    assert all(row["label"] != "Also starts sharing" for row in rows)
    # One server row per server the press shares: the button counts these.
    assert [r["label"] for r in rows if r.get("field") == "server"] == [
        "slack-mcp",
        "chorus-mcp",
        "grasp-mcp",
    ]
    # Each other server's commands and settings follow its own row, whole.
    chorus = rows.index(
        {"field": "server", "label": "chorus-mcp", "before": "One per chat", "after": "Shared"}
    )
    grasp = rows.index(
        {"field": "server", "label": "grasp-mcp", "before": "One per chat", "after": "Shared"}
    )
    assert rows[chorus + 1 : grasp] == [
        {"label": "Command", "after": "/opt/chorus  --stdio"},
        {"label": "Setting", "after": "LOG_LEVEL=warn"},
        {"label": "Setting", "after": "TOKEN=<hidden sha256:11>"},
    ]
    assert rows[grasp + 1 :] == [
        {"label": "Command", "after": "/opt/grasp --profile work"},
        {"label": "Command", "after": "/opt/grasp --profile home"},
        {"label": "Setting", "after": "LD_PRELOAD=/tmp/x.so"},
    ]


def test_a_marked_server_that_will_not_start_is_not_named():
    """An unapproved or changed marked server stays one per chat, so the card does not claim it."""
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(
            sharing_on=False,
            other_marked=["chorus-mcp", "grasp-mcp"],
            other_launches=[_CHORUS],
        ),
    )

    assert preview["title"] == "Share slack-mcp, chorus-mcp"
    assert all(row["label"] != "grasp-mcp" for row in preview["changes"])
    assert preview["apply"][1]["body"] == {"enabled": True}


def test_no_other_server_starting_keeps_a_one_server_card():
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(sharing_on=False, other_marked=["grasp-mcp"], other_launches=[]),
    )

    assert preview["title"] == "Share slack-mcp"
    assert [r["label"] for r in preview["changes"] if r.get("field") == "server"] == ["slack-mcp"]


@pytest.mark.parametrize(
    "other_launches",
    [
        None,
        "chorus-mcp",
        [{"name": "chorus-mcp"}],
        [{"name": "chorus-mcp", "launches": []}],
        [{"name": "chorus-mcp", "launches": [{"command": "", "env": []}]}],
        [{"name": "chorus-mcp", "launches": [{"command": "/opt/c", "env": [1]}]}],
        [{"name": "other-mcp", "launches": [{"command": "/opt/o", "env": []}]}],
    ],
)
def test_another_server_whose_launch_cannot_be_shown_refuses_the_card(other_launches):
    """Turning sharing on would start it unseen, so the press is not offered."""
    snap = _snap(sharing_on=False, other_marked=["chorus-mcp"])
    if other_launches is not None:
        snap["other_launches"] = other_launches
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"


def test_a_changed_other_launch_counts_as_a_change_since_preview():
    """The press is refused when anything the card showed for another server moved."""
    from kiro_crew.dashboard import change_cards

    def key(snap):
        return change_cards.canonical(
            change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, snap)
        )

    before = _snap(sharing_on=False, other_marked=["chorus-mcp"], other_launches=[_CHORUS])
    moved_cmd = {
        "name": "chorus-mcp",
        "launches": [{"command": "/opt/chorus --evil", "env": _CHORUS["launches"][0]["env"]}],
    }
    for now in (
        _snap(sharing_on=False, other_marked=["chorus-mcp"], other_launches=[moved_cmd]),
        _snap(sharing_on=False, other_marked=["chorus-mcp"], other_launches=[]),
        _snap(sharing_on=False, other_marked=["chorus-mcp", "grasp-mcp"], other_launches=[_CHORUS]),
    ):
        assert key(before) != key(now)


def test_a_moving_memory_reading_is_not_a_change_since_preview():
    """RSS moves between preview and press; the card must still apply."""
    from kiro_crew.dashboard import change_cards

    before = _snap(saves_bytes=100_000_000)
    now = _snap(saves_bytes=120_000_000)
    same = change_cards.canonical(change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, before))
    assert same == change_cards.canonical(
        change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, now)
    )
    # A real change still counts.
    moved = _snap(saves_bytes=100_000_000, expected_launch="c" * 64)
    assert same != change_cards.canonical(
        change_cards.comparable_snapshot(catalog.KIND_MCP_SHARE, moved)
    )


def test_the_connection_routes_stay_hooked():
    assert ("POST", "/api/connections/mint") in catalog.HOOKED_ROUTES
    assert ("GET", "/api/connections/mint") in catalog.HOOKED_ROUTES


def test_a_share_left_with_the_switch_off_is_not_reported_as_done():
    from kiro_crew.dashboard import change_cards

    p = {"server": "slack-mcp", "shared": True}
    assert not change_cards.after_shows_the_write(
        catalog.KIND_MCP_SHARE, p, {"shared": True, "sharing_on": False}
    )
    assert change_cards.after_shows_the_write(
        catalog.KIND_MCP_SHARE, p, {"shared": True, "sharing_on": True}
    )


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("credential_vending", "Can't share: gives each chat its own credentials"),
        ("needs_tenant_nonce", "Can't share: each chat needs its own copy"),
        ("computer_use", "Can't share: it controls this computer"),
    ],
)
def test_each_leakage_class_is_refused_with_one_line(code, line):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "creds-agent", "shared": True}, _snap(share_blocked=code))

    assert exc.value.code == "share_refused"
    assert str(exc.value) == line


def test_sharing_shows_the_launch_and_is_rated_as_running_code():
    """The shared copy runs outside the chat sandbox: show and pin what it runs."""
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert preview["risk"] == catalog.RISK_CODE_EXEC
    assert {"label": "Command", "after": "/opt/slack --stdio"} in preview["changes"]
    assert {"label": "Setting", "after": "SLACK_TOKEN=<hidden sha256:00>"} in preview["changes"]


@pytest.mark.parametrize(
    "over",
    [{"expected_launch": None}, {"launch_commands": []}, {"expected_launch": ""}],
)
def test_a_launch_that_cannot_be_shown_cannot_be_shared(over):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, _snap(**over))
    assert exc.value.code == "launch_unreadable"


def test_stopping_sharing_is_tightening_and_unpinned():
    preview = _preview({"server": "slack-mcp", "shared": False}, _snap(shared=True))
    assert preview["risk"] == catalog.RISK_TIGHTEN
    assert preview["apply"][0]["body"] == {"name": "slack-mcp", "stub": False}


def test_turning_a_refused_class_off_is_allowed():
    preview = _preview(
        {"server": "creds-agent", "shared": False},
        _snap(shared=True, share_blocked="credential_vending"),
    )
    assert preview["apply"][0]["body"] == {"name": "creds-agent", "stub": False}


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (_snap(exists=False), "not_found"),
        (_snap(can_share=False), "not_shareable"),
        (_snap(shared=True), "no_change"),
    ],
)
def test_a_card_that_changes_nothing_is_refused(snapshot, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snapshot)
    assert exc.value.code == code


def test_the_saving_row_is_the_measured_number():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap(saves_bytes=240_000_000))

    assert {"label": "Saves", "after": "240 MB"} in preview["changes"]


def test_no_saving_row_without_a_measurement():
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap())

    assert all(row["label"] != "Saves" for row in preview["changes"])


def test_undo_restores_the_state_before_the_press():
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": True})
    plan, reason = catalog.build_undo(catalog.KIND_MCP_SHARE, p, {"shared": False}, [{}], 1)

    assert reason is None
    assert plan == [
        {
            "method": "POST",
            "path": "/api/mcp-gateway/servers/stub",
            "body": {"name": "slack-mcp", "stub": False},
        }
    ]


def test_undoing_a_share_that_turned_sharing_on_turns_it_off_again():
    """The card's enable step started every marked server; Undo must not leave that running."""
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": True})
    before = {"shared": False, "sharing_on": False}

    plan, reason = catalog.build_undo(catalog.KIND_MCP_SHARE, p, before, [{}, {}], 2)

    assert reason is None
    assert plan == [
        {
            "method": "POST",
            "path": "/api/mcp-gateway/servers/stub",
            "body": {"name": "slack-mcp", "stub": False},
        },
        {"method": "POST", "path": "/api/mcp-gateway/enable", "body": {"enabled": False}},
    ]
    # Only the share step applied: sharing was never turned on, so leave it alone.
    plan, _ = catalog.build_undo(catalog.KIND_MCP_SHARE, p, before, [{}], 1)
    assert len(plan) == 1


@pytest.mark.parametrize(
    ("current", "stale"),
    [
        ({"shared": False, "sharing_on": True}, False),
        ({"shared": True, "sharing_on": True}, True),
        ({"shared": False, "sharing_on": False}, True),
    ],
)
def test_resumed_undo_turns_sharing_off_only_after_the_unshare(monkeypatch, current, stale):
    """Undo's second step runs through the resumed-step gate; a refusal would leave sharing on."""
    import asyncio

    from kiro_crew.dashboard import change_cards

    async def fake_read_state(kind, params, evidence, *, state, app):
        return {"exists": True, **current}

    monkeypatch.setattr(change_cards, "read_state", fake_read_state)
    rec = {
        "kind": catalog.KIND_MCP_SHARE,
        "params": {"server": "slack-mcp", "shared": True},
        "after": {"shared": True, "sharing_on": True},
        "evidence": [{}, {}],
    }

    got = asyncio.run(change_cards.resumed_undo_is_stale(rec, 1, state=None, app=None))

    assert got is stale


def test_undoing_stop_sharing_never_shares_again_unseen():
    """Re-sharing approves a launch outside the sandbox, so Undo does not offer it."""
    p = catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": False})
    before = {"shared": True, "expected_launch": _EXPECTED}
    plan, reason = catalog.build_undo(catalog.KIND_MCP_SHARE, p, before, [{}], 1)

    assert plan is None and reason == "reshare_needs_review"


def test_a_long_command_is_shown_whole():
    long_cmd = "/bin/sh -c " + "x" * 400 + " && suffix"
    preview = _preview({"server": "slack-mcp", "shared": True}, _snap(launch_commands=[long_cmd]))

    assert {"label": "Command", "after": long_cmd} in preview["changes"]


def test_the_manual_toggle_is_remembered_by_name_only():
    line = catalog.describe_manual_change(
        ("POST", "/api/mcp-gateway/servers/stub"), {}, {"name": "slack-mcp", "stub": True}
    )
    assert line == "shared MCP server slack-mcp across chats"


def test_params_are_validated():
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(catalog.KIND_MCP_SHARE, {"server": "slack-mcp"})
    with pytest.raises(catalog.CardCatalogError):
        catalog.validate_params(
            catalog.KIND_MCP_SHARE, {"server": "slack-mcp", "shared": True, "extra": 1}
        )


def test_the_snapshot_reads_the_same_sources_as_the_page(monkeypatch):
    from kiro_crew.dashboard import change_cards
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import direct_copies

    monkeypatch.setattr(
        mcp_mod,
        "_collect_server_rows",
        lambda: {"slack-mcp": {"transport": "stdio", "commands": {("/opt/slack",)}}},
    )
    monkeypatch.setattr(
        direct_copies,
        "measure",
        lambda commands: {"slack-mcp": direct_copies.DirectCopies(2, 200_000_000)},
    )
    monkeypatch.setattr(
        mcp_mod.launch_resolve,
        "resolve_launches",
        lambda names, refresh_specs: mcp_mod.launch_resolve.ResolvedLaunches({"slack-mcp": ()}),
    )
    monkeypatch.setattr(
        mcp_mod,
        "display_launches",
        lambda name, launches: {
            "commands": [["/opt/slack", "--stdio"]],
            "envs": [["SLACK_TOKEN=<hidden sha256:00>"]],
            "complete": True,
            "expected_launch": _EXPECTED,
        },
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert snap["exists"] and snap["can_share"]
    assert snap["share_blocked"] is None
    assert snap["saves_bytes"] == 100_000_000
    assert snap["launch_commands"] == ["/opt/slack --stdio"]
    assert snap["launch_env_lines"] == ["SLACK_TOKEN=<hidden sha256:00>"]
    assert snap["expected_launch"] == _EXPECTED
    assert change_cards._mcp_share_snapshot("missing-mcp") == {"exists": False}


def _launch(command, args, env, *, tag):
    from kiro_crew.mcp_gateway.launch_approval import ResolvedLaunch

    cmd_hash, env_hash = f"c{tag}" * 8, f"e{tag}" * 8
    return ResolvedLaunch(
        f"{cmd_hash}:{env_hash}", command, tuple(args), frozenset({env_hash}), tuple(env)
    )


def _patch_others(
    monkeypatch,
    *,
    sharing_on,
    roster,
    others,
    approved,
    calls,
    over_cap=(),
    complete=True,
    target_over_cap=False,
    target_complete=True,
):
    """Wire the snapshot to a fake roster, resolver and approval store."""
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import mcp as mcp_mod
    from kiro_crew.mcp_gateway import direct_copies, launch_approval
    from kiro_crew.mcp_gateway.launch_resolve import ResolvedLaunches

    real = KiroCrewConfig.load()
    cfg = dataclasses.replace(
        real,
        mcp_gateway=dataclasses.replace(
            real.mcp_gateway, enabled=sharing_on, stub_servers=list(roster)
        ),
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(
        mcp_mod,
        "_collect_server_rows",
        lambda: {n: {"transport": "stdio", "commands": set()} for n in ["slack-mcp", *roster]},
    )
    monkeypatch.setattr(direct_copies, "measure", lambda commands: {})

    def resolve(names, *, cfg=None, refresh_specs=True):
        calls.append((list(names), None if cfg is None else cfg.mcp_gateway.enabled))
        if list(names) == ["slack-mcp"]:
            if target_over_cap:
                return ResolvedLaunches(over_cap=["slack-mcp"], complete=target_complete)
            return ResolvedLaunches(
                {"slack-mcp": [_launch("/opt/slack", ["--stdio"], [], tag="s")]},
                complete=target_complete,
            )
        return ResolvedLaunches(
            {n: others[n] for n in names if n in others and n not in over_cap},
            over_cap=[n for n in names if n in over_cap],
            complete=complete,
        )

    monkeypatch.setattr(mcp_mod.launch_resolve, "resolve_launches", resolve)
    store = launch_approval.LaunchApprovals(
        approved={launch_approval.target_stem(n): set(fps) for n, fps in approved.items()}
    )
    monkeypatch.setattr(launch_approval, "load_approvals", lambda *a, **k: store)


def test_the_snapshot_shows_every_approved_launch_that_sharing_starts(monkeypatch):
    """Only launches the gateway would actually run are listed, each with its whole env."""
    from kiro_crew.dashboard import change_cards

    chorus = _launch(
        "/opt/chorus",
        ["--name", "a  b\u202e"],
        [("NODE_OPTIONS", "--require  /tmp/x.js"), ("TOKEN", "s3cr3t-value")],
        tag="1",
    )
    grasp_ok = _launch("/opt/grasp", ["--profile", "work"], [], tag="2")
    grasp_new = _launch("/opt/grasp", ["--profile", "evil"], [], tag="3")
    calls: list = []
    _patch_others(
        monkeypatch,
        sharing_on=False,
        roster=["chorus-mcp", "grasp-mcp", "taskei-mcp", "notion-mcp"],
        # notion-mcp resolves to nothing the gateway would run (unwrapped, or
        # withheld env once sharing is on); taskei-mcp was never approved.
        others={
            "chorus-mcp": [chorus],
            "grasp-mcp": [grasp_ok, grasp_new],
            "taskei-mcp": [_launch("/opt/taskei", [], [], tag="4")],
        },
        approved={
            "chorus-mcp": {chorus.fingerprint},
            "grasp-mcp": {grasp_ok.fingerprint},
        },
        calls=calls,
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    # The others are resolved as the gateway resolves them once sharing is on.
    assert (["chorus-mcp", "grasp-mcp", "notion-mcp", "taskei-mcp"], True) in calls
    assert snap["other_marked"] == ["chorus-mcp", "grasp-mcp", "notion-mcp", "taskei-mcp"]
    assert snap["other_launches"] == [
        {
            "name": "chorus-mcp",
            "launches": [
                {
                    "command": "/opt/chorus --name 'a  b\\u{202e}'",
                    "env": [
                        "NODE_OPTIONS=--require  /tmp/x.js",
                        f"TOKEN=<hidden sha256:{_digest8('s3cr3t-value')}>",
                    ],
                }
            ],
        },
        {"name": "grasp-mcp", "launches": [{"command": "/opt/grasp --profile work", "env": []}]},
    ]
    assert "s3cr3t-value" not in repr(snap)
    preview = _preview({"server": "slack-mcp", "shared": True}, snap)
    assert preview["title"] == "Share slack-mcp, chorus-mcp, grasp-mcp"
    assert {"label": "Command", "after": "/opt/grasp --profile evil"} not in preview["changes"]


def test_the_snapshot_withholds_others_whose_launch_cannot_be_read(monkeypatch):
    from kiro_crew.dashboard import change_cards

    chorus = _launch("/opt/chorus", ["x" * 20_000], [], tag="1")
    _patch_others(
        monkeypatch,
        sharing_on=False,
        roster=["chorus-mcp"],
        others={"chorus-mcp": [chorus]},
        approved={"chorus-mcp": {chorus.fingerprint}},
        calls=[],
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert snap["other_launches"] is None
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"


def test_the_snapshot_skips_the_others_when_sharing_is_already_on(monkeypatch):
    from kiro_crew.dashboard import change_cards

    calls: list = []
    _patch_others(
        monkeypatch,
        sharing_on=True,
        roster=["chorus-mcp"],
        others={"chorus-mcp": [_launch("/opt/chorus", [], [], tag="1")]},
        approved={},
        calls=calls,
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert "other_launches" not in snap
    assert [names for names, _ in calls] == [["slack-mcp"]]


def test_every_env_value_the_card_approves_is_on_the_card():
    """A value such as LD_PRELOAD is part of what runs outside the sandbox."""
    preview = _preview(
        {"server": "slack-mcp", "shared": True},
        _snap(launch_env_lines=["LD_PRELOAD=/tmp/x.so", "API_KEY=<hidden sha256:00>"]),
    )

    assert {"label": "Setting", "after": "LD_PRELOAD=/tmp/x.so"} in preview["changes"]
    assert {"label": "Setting", "after": "API_KEY=<hidden sha256:00>"} in preview["changes"]


def test_hidden_characters_in_a_launch_are_escaped_for_the_card():
    from kiro_crew.dashboard import change_cards

    assert change_cards._visible("/opt/a\u202egnp.exe") == "/opt/a\\u{202e}gnp.exe"
    assert change_cards._visible("a\u200bb\tc") == "a\\u{200b}b\\x09c"
    assert change_cards._visible("plain é") == "plain é"


# -- sharing never starts a launch the card could not show whole ------------------


@pytest.mark.parametrize("approved_too", [True, False])
def test_another_server_past_the_launch_cap_refuses_the_card(monkeypatch, approved_too):
    """A capped server has no entry, yet sharing would start the launches that fit."""
    from kiro_crew.dashboard import change_cards

    chorus = _launch("/opt/chorus", [], [], tag="1")
    grasp = _launch("/opt/grasp", [], [], tag="2")
    _patch_others(
        monkeypatch,
        sharing_on=False,
        roster=["chorus-mcp", "grasp-mcp"],
        others={"chorus-mcp": [chorus], "grasp-mcp": [grasp]},
        approved={
            "chorus-mcp": {chorus.fingerprint},
            **({"grasp-mcp": {grasp.fingerprint}} if approved_too else {}),
        },
        calls=[],
        over_cap=["grasp-mcp"],
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert snap["other_launches"] is None
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"


def test_another_server_resolved_from_an_unread_source_refuses_the_card(monkeypatch):
    from kiro_crew.dashboard import change_cards

    chorus = _launch("/opt/chorus", [], [], tag="1")
    _patch_others(
        monkeypatch,
        sharing_on=False,
        roster=["chorus-mcp"],
        others={"chorus-mcp": [chorus]},
        approved={"chorus-mcp": {chorus.fingerprint}},
        calls=[],
        complete=False,
    )

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert snap["other_launches"] is None
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"


@pytest.mark.parametrize(
    "over", [{"target_over_cap": True}, {"target_complete": False}], ids=["capped", "unread"]
)
def test_a_server_whose_launches_were_not_all_seen_cannot_be_shared(monkeypatch, over):
    from kiro_crew.dashboard import change_cards

    _patch_others(monkeypatch, sharing_on=True, roster=[], others={}, approved={}, calls=[], **over)

    snap = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert "expected_launch" not in snap
    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"


def _stale(rec):
    import asyncio

    from kiro_crew.dashboard import change_cards

    return asyncio.run(change_cards.resumed_apply_is_stale(rec, 1, state=_test_state(), app=None))


def _share_rec(before):
    return {
        "kind": catalog.KIND_MCP_SHARE,
        "params": {"server": "slack-mcp", "shared": True},
        "before": before,
        "evidence": [{}],
    }


_L_CHORUS = _launch("/opt/chorus", ["--stdio"], [("TOKEN", "t1")], tag="1")


def _preview_world(monkeypatch, **over):
    """Before the press: sharing off, chorus-mcp marked and approved."""
    kw = {
        "sharing_on": False,
        "roster": ["chorus-mcp"],
        "others": {"chorus-mcp": [_L_CHORUS]},
        "approved": {"chorus-mcp": {_L_CHORUS.fingerprint}},
        "calls": [],
    }
    kw.update(over)
    _patch_others(monkeypatch, **kw)


def _after_step0(monkeypatch, **over):
    """What step 0 legitimately made: slack-mcp marked; everything else as previewed."""
    kw = {"roster": ["chorus-mcp", "slack-mcp"]}
    kw.update(over)
    _preview_world(monkeypatch, **kw)


def test_the_enable_step_is_admitted_when_only_the_share_landed(monkeypatch):
    from kiro_crew.dashboard import change_cards

    _preview_world(monkeypatch)
    before = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())
    assert before["other_launches"] and before["expected_launch"]
    _after_step0(monkeypatch)

    assert _stale(_share_rec(before)) is False


_L_EVIL = _launch("/opt/chorus", ["--stdio", "--evil"], [("TOKEN", "t1")], tag="9")
_L_NEW_TOKEN = _launch("/opt/chorus", ["--stdio"], [("TOKEN", "t2")], tag="8")
_L_GRASP = _launch("/opt/grasp", [], [], tag="2")


@pytest.mark.parametrize(
    "drift",
    [
        # Another marked server's approved launch runs different args.
        {"others": {"chorus-mcp": [_L_EVIL]}, "approved": {"chorus-mcp": {_L_EVIL.fingerprint}}},
        # ... or a different env value.
        {
            "others": {"chorus-mcp": [_L_NEW_TOKEN]},
            "approved": {"chorus-mcp": {_L_NEW_TOKEN.fingerprint}},
        },
        # A second launch of the same server was approved in between.
        {
            "others": {"chorus-mcp": [_L_CHORUS, _L_EVIL]},
            "approved": {"chorus-mcp": {_L_CHORUS.fingerprint, _L_EVIL.fingerprint}},
        },
        # A new server was marked, approved and would start with the switch.
        {
            "roster": ["chorus-mcp", "grasp-mcp", "slack-mcp"],
            "others": {"chorus-mcp": [_L_CHORUS], "grasp-mcp": [_L_GRASP]},
            "approved": {
                "chorus-mcp": {_L_CHORUS.fingerprint},
                "grasp-mcp": {_L_GRASP.fingerprint},
            },
        },
        # The approval behind the previewed launch was revoked.
        {"approved": {}},
        # The other server's launches cannot be enumerated whole.
        {"over_cap": ["chorus-mcp"]},
        {"complete": False},
        # The switch was turned on elsewhere.
        {"sharing_on": True},
        # The shared server's own launch moved.
        {"target_over_cap": True},
    ],
    ids=[
        "args",
        "env",
        "added-launch",
        "added-server",
        "revoked",
        "capped",
        "unread",
        "switch-on",
        "target",
    ],
)
def test_the_enable_step_is_refused_when_anything_it_starts_moved(monkeypatch, drift):
    from kiro_crew.dashboard import change_cards

    _preview_world(monkeypatch)
    before = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())
    _after_step0(monkeypatch, **drift)

    assert _stale(_share_rec(before)) is True


def test_the_enable_step_is_refused_when_the_share_did_not_land(monkeypatch):
    from kiro_crew.dashboard import change_cards

    _preview_world(monkeypatch)
    before = change_cards._mcp_share_snapshot("slack-mcp", state=_test_state())

    assert _stale(_share_rec(before)) is True


def test_only_a_share_cards_later_step_is_rechecked():
    import asyncio

    from kiro_crew.dashboard import change_cards

    rec = {"kind": "crewmate.capabilities", "params": {}, "before": {}, "evidence": [{}]}
    assert asyncio.run(change_cards.resumed_apply_is_stale(rec, 1, state=None, app=None)) is False
    # A share card whose preview recorded no server is never enabled.
    assert _stale(_share_rec({"exists": False})) is True
    zero = asyncio.run(change_cards.resumed_apply_is_stale(_share_rec({}), 0, state=None, app=None))
    assert zero is False


@pytest.mark.parametrize("other", ["kirocrew-core", "chorus-mcp"])
def test_card_shows_a_served_server_pending_removal(monkeypatch, other):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard import change_cards

    launch = _launch("/opt/other", ["--stdio"], [], tag="1")
    _patch_others(
        monkeypatch,
        sharing_on=False,
        roster=[],
        others={other: [launch]},
        approved={other: {launch.fingerprint}},
        calls=[],
    )
    state = SimpleNamespace(_mcp_gateway_serving=lambda: frozenset({"slack-mcp", other}))
    snap = asyncio.run(
        change_cards.read_state(
            catalog.KIND_MCP_SHARE,
            {"server": "slack-mcp", "shared": True},
            [],
            state=state,
            app=None,
        )
    )

    assert snap["other_launches"] == [
        {"name": other, "launches": [{"command": "/opt/other --stdio", "env": []}]}
    ]
    assert other in _preview({"server": "slack-mcp", "shared": True}, snap)["title"]


@pytest.mark.parametrize("sharing_on", [False, True])
@pytest.mark.parametrize("serving", [None, frozenset(), frozenset({"chorus-mcp"})])
def test_card_refuses_unknown_or_inactive_target(monkeypatch, serving, sharing_on):
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.dashboard import change_cards

    _patch_others(monkeypatch, sharing_on=sharing_on, roster=[], others={}, approved={}, calls=[])
    state = SimpleNamespace(_mcp_gateway_serving=lambda: serving)
    snap = asyncio.run(
        change_cards.read_state(
            catalog.KIND_MCP_SHARE,
            {"server": "slack-mcp", "shared": True},
            [],
            state=state,
            app=None,
        )
    )

    with pytest.raises(catalog.CardCatalogError) as exc:
        _preview({"server": "slack-mcp", "shared": True}, snap)
    assert exc.value.code == "launch_unreadable"
