from __future__ import annotations
import asyncio
import pytest
from chat_test_helpers import _make_state
from kiro_crew.dashboard import session_control as sc, create_rate_limit
from kiro_crew.dashboard.chat_utils import slot_history_key
from test_session_control import _agent_resolves

@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()

def _key(slot): return slot_history_key(slot)
def _root(state, name="chat-1"):
    s=state.get_or_create_slot(name); return s
def _create(state, caller, **kw):
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller), **kw))

def test_positive_two_descendants_and_status_unknown_action(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    c1=_create(state, root); child=state.get_slot(c1["target"])
    assert child._session_depth==1 and child._session_leaf is False
    g1=_create(state, child); grand=state.get_slot(g1["target"])
    assert grand._session_depth==2 and grand._session_root_key==root.key
    # depth denial: grandchild would be depth 3
    with pytest.raises(sc.SessionControlError) as e: _create(state, grand)
    assert e.value.code=="lineage_depth_exceeded"
    assert grand._session_max_depth==2
    assert not getattr(grand, "_current_action", "")

def test_leaf_denial(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    c=_create(state, root, leaf=True); leaf=state.get_slot(c["target"])
    assert leaf._session_leaf is True
    with pytest.raises(sc.SessionControlError) as e: _create(state, leaf)
    assert e.value.code=="leaf_session_caller"

def test_tree_quota_and_escalation_denial(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    root._session_max_descendants=2; root._session_depth=0; root._session_root_key=root.key
    _create(state, root); _create(state, root)
    with pytest.raises(sc.SessionControlError) as e: _create(state, root)
    assert e.value.code=="tree_descendant_quota_exceeded"
    with pytest.raises(sc.SessionControlError) as e2:
        sc._creation_allowance(state, root, requested_max_descendants=99)
    assert e2.value.code=="session_limit_escalation"

def test_cyclic_and_missing_lineage_fail_closed(tmp_path):
    state=_make_state(tmp_path); a=_root(state,"chat-a"); b=state.get_or_create_slot("chat-b")
    a._created_by=b.key; b._created_by=a.key
    with pytest.raises(sc.SessionControlError) as e: sc._creation_allowance(state, a)
    assert e.value.code=="lineage_cycle"
    # Grandfather: missing/archived ancestor is a historic boundary; stored
    # depth/limits on the visited slot still bound creation.
    c=state.get_or_create_slot("chat-c"); c._created_by="chat-missing"
    allow=sc._creation_allowance(state, c)
    assert allow["depth"]==1  # legacy grandfather: highest live ancestor is itself

def test_sibling_cross_workspace_and_member_fence_still_deny(tmp_path):
    state=_make_state(tmp_path); a=_root(state,"chat-a"); b=state.get_or_create_slot("chat-b")
    child=state.get_or_create_slot("chat-child"); child._created_by=a.key; child.workspace="default"; b.workspace="other"
    with pytest.raises(sc.SessionControlError) as e:
        sc.authorize_target(state, caller_session_key=_key(b), target="chat-child", operation="read")
    assert e.value.code in {"workspace_mismatch","not_creator"}

def test_fork_quota_escape_denied(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    root._session_leaf=True
    with pytest.raises(sc.SessionControlError) as e:
        asyncio.run(sc.fork_session(state, caller_session_key=_key(root)))
    assert e.value.code=="leaf_session_caller"

def test_status_row_current_action_unknown_unit(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    c=_create(state, root)
    result=asyncio.run(sc.created_session_status(state, caller_session_key=_key(root)))
    rows=result.get("sessions") or result.get("rows") or []
    assert rows, result.keys()
    row=[r for r in rows if r.get("target")==c["target"]][0]
    assert row["current_action"]=="unknown" and row["current_action_source"]=="unknown"
    assert row["leaf"] is False and row["depth"]==1

def test_deadline_creation_admission_only(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    c=_create(state, root, budget_seconds=60); child=state.get_slot(c["target"])
    assert child._session_deadline_at is not None
    child._session_deadline_at=0
    with pytest.raises(sc.SessionControlError) as e: _create(state, child)
    assert e.value.code=="session_budget_exhausted"
    with pytest.raises(sc.SessionControlError) as e2:
        asyncio.run(sc.create_session(state, caller_session_key=_key(root), budget_seconds="60"))
    assert e2.value.code=="invalid_field_type"
    with pytest.raises(sc.SessionControlError) as e3:
        asyncio.run(sc.create_session(state, caller_session_key=_key(root), max_depth=True))
    assert e3.value.code=="invalid_field_type"

def test_codec_roundtrip_and_rehydrated_leaf(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
    from test_slot_metadata_codec import _slot_folds, _purpose
    import json as _json
    state=_make_state(tmp_path); slot=state.get_or_create_slot("chat-src")
    slot._session_leaf=True; slot._session_depth=1; slot._session_root_key="chat-1"
    slot._session_max_depth=1; slot._session_max_descendants=3; slot._session_deadline_at=123.5
    meta=_json.loads(_json.dumps(codec.encode(slot, folds=_slot_folds(slot))))
    assert meta["session_leaf"] is True and meta["session_max_depth"]==1
    fresh=state.get_or_create_slot("chat-dst")
    codec.apply(state, fresh, meta, _purpose(codec.RESTORE, name="chat-dst"))
    assert fresh._session_leaf is True and fresh._session_depth==1 and fresh._session_max_descendants==3 and fresh._session_deadline_at==123.5
    with pytest.raises(sc.SessionControlError) as e: sc._creation_allowance(state, fresh)
    assert e.value.code=="leaf_session_caller"
    with pytest.raises(ValueError):
        codec._read_session_capability(type("R",(),{"meta":{"session_depth":True},"slot":fresh})())

@pytest.mark.parametrize("meta", [
    {"session_root_key":"chat-x"}, {"session_depth":1}, {"session_max_depth":99},
    {"session_depth":"1","session_root_key":"chat-x"}, {"session_deadline_at":float("nan"),"session_depth":1,"session_root_key":"chat-x"},
])
def test_partial_or_corrupt_capability_metadata_fail_closed(tmp_path, meta):
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
    state=_make_state(tmp_path); slot=state.get_or_create_slot("chat-z")
    with pytest.raises(ValueError):
        codec._read_session_capability(type("R",(),{"meta":meta,"slot":slot})())

def test_edited_root_after_rehydrate_fails_create_gate(tmp_path, monkeypatch):
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
    from test_slot_metadata_codec import _slot_folds, _purpose
    import json as _json
    state=_make_state(tmp_path); slot=state.get_or_create_slot("chat-src")
    slot._session_leaf=False; slot._session_depth=1; slot._session_root_key="chat-forged"
    slot._session_max_depth=2; slot._session_max_descendants=8; slot._session_deadline_at=None
    meta=_json.loads(_json.dumps(codec.encode(slot, folds=_slot_folds(slot))))
    fresh=state.get_or_create_slot("chat-dst2")
    codec.apply(state, fresh, meta, _purpose(codec.RESTORE, name="chat-dst2"))
    assert fresh._session_root_key=="chat-forged"
    with pytest.raises(sc.SessionControlError) as e: sc._creation_allowance(state, fresh)
    assert e.value.code in {"lineage_root_unverified","lineage_root_mismatch"}

def test_mcp_payload_forwards_limits():
    from test_mcp_dashboard_folders import _call
    out, dash=_call("session_create", {"title":"w","leaf":True,"max_depth":1,"max_descendants":2,"budget_seconds":60}, {"POST /api/session-control/create":{"target":"chat-9","title":"w"}})
    assert dash.requests, out
    body=dash.requests[-1].body
    assert body["leaf"] is True and body["max_depth"]==1 and body["max_descendants"]==2 and body["budget_seconds"]==60

def test_http_handler_forwards_and_rejects_bad_types(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from kiro_crew.dashboard.handlers import session_control as handlers_sc
    _agent_resolves(monkeypatch,"default")
    state=_make_state(tmp_path); caller=state.get_or_create_slot("chat-1"); state.get_or_create_slot("chat-2")
    def req(body):
        r=MagicMock(); r.app={"state":state}; r.path="/api/session-control/create"; r.method="POST"
        r.headers={"X-Session-Key":_key(caller)}; r.query={}
        r.get=lambda k,d=None: True if k in ("internal_auth","peer_verified") else d
        async def _json(): return body
        r.json=_json; return r
    resp=asyncio.run(handlers_sc.api_session_control_create(req({"title":"w","leaf":True,"max_depth":1})))
    assert resp.status==200
    import json as _json; created=state.get_slot(_json.loads(resp.body.decode())["target"])
    assert created._session_leaf is True and created._session_max_depth==1
    resp2=asyncio.run(handlers_sc.api_session_control_create(req({"title":"w","max_depth":True})))
    assert resp2.status!=200 and _json.loads(resp2.body.decode())["code"]=="invalid_field_type"

def test_two_orphan_branches_share_original_root_quota(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    root._session_max_descendants=3; root._session_depth=0; root._session_root_key=root.key
    # Seed gateway-authored crew-log tree so archived-root corroboration can prove A/B's root
    from test_session_control_tree import _live, _hold
    from kiro_crew.crew_log import emit as _emit
    from kiro_crew.crew_log import session_tree_projection as stp
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome")); monkeypatch.setenv(_emit.CREW_LOG_ENV, "1")
    stp.reset_for_tests(); stp.projection().ensure_seeded(); sc._tree_pending_slots.clear()
    _live(state, root, "sid-root")
    a=_create(state, root); b=_create(state, root)
    A=state.get_slot(a["target"]); B=state.get_slot(b["target"])
    assert A._session_root_key==root.key and B._session_root_key==root.key
    _live(state, A, "sid-a"); _live(state, B, "sid-b"); _hold(state, A.key, root.key, sid="sid-a"); _hold(state, B.key, root.key, sid="sid-b")
    del state._slots[root.key]  # root archived/closed
    _create(state, A)  # total live under original root = 3 (A,B,new)
    with pytest.raises(sc.SessionControlError) as e: _create(state, B)
    assert e.value.code=="tree_descendant_quota_exceeded"

def test_forged_capability_cannot_exceed_system_ceilings(tmp_path):
    state=_make_state(tmp_path); c=state.get_or_create_slot("chat-c")
    c._created_by="chat-missing"; c._session_depth=0; c._session_max_depth=99; c._session_max_descendants=99; c._session_root_key="chat-missing"
    # Managed stamp whose root no authoritative source can prove: fail closed, not clamped-and-trusted
    with pytest.raises(sc.SessionControlError) as e: sc._creation_allowance(state, c)
    assert e.value.code=="lineage_root_unverified"
    from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
    with pytest.raises(ValueError):
        codec._read_session_capability(type("R",(),{"meta":{"session_max_depth":99},"slot":c})())
    with pytest.raises(ValueError):
        codec._read_session_capability(type("R",(),{"meta":{"session_deadline_at":float("inf")},"slot":c})())

def test_nan_inf_budget_rejected(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    for bad in (float("nan"), float("inf")):
        with pytest.raises(sc.SessionControlError) as e:
            asyncio.run(sc.create_session(state, caller_session_key=_key(root), budget_seconds=bad))
        assert e.value.code=="invalid_field_type"

def test_fork_mcp_payload_forwards_leaf():
    from test_mcp_dashboard_folders import _call
    out, dash=_call("session_fork", {"title":"w","leaf":True}, {"POST /api/session-control/fork":{"target":"chat-9","source":"chat-1","title":"w","messages":1}})
    assert dash.requests[-1].body["leaf"] is True

def test_adopt_cross_root_refused_and_same_root_restamps(tmp_path, monkeypatch):
    from test_session_control_tree import _live, _hold, _emitted
    from kiro_crew.crew_log import emit as _emit
    from kiro_crew.crew_log import session_tree_projection as stp
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    monkeypatch.setenv(_emit.CREW_LOG_ENV, "1")
    stp.reset_for_tests(); stp.projection().ensure_seeded(); sc._tree_pending_slots.clear()
    calls=_emitted(monkeypatch)
    state=_make_state(tmp_path)
    caller=_live(state, state.get_or_create_slot("chat-caller"), "sid-caller")
    target=_live(state, state.get_or_create_slot("chat-target"), "sid-target")
    caller._session_root_key="chat-caller"; caller._session_depth=0
    target._created_by="chat-other"; target._session_root_key="chat-other"; target._session_depth=1
    with pytest.raises(sc.SessionControlError) as e:
        asyncio.run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-target"))
    assert e.value.code=="adopt_cross_root_refused"
    # same-root: clear foreign root, adoption succeeds and re-stamps root/depth
    target._created_by=caller.key; target._session_root_key="chat-caller"
    caller._session_deadline_at=9999999999.0; target._session_deadline_at=99999999999.0
    result=asyncio.run(sc.adopt_target(state, caller_session_key=_key(caller), target="chat-target"))
    assert result["target"]=="chat-target"
    assert target._session_root_key=="chat-caller" and target._session_depth==1
    assert target._session_deadline_at==9999999999.0  # min(existing, adopter allowance)

def test_concurrent_creates_respect_quota(tmp_path, monkeypatch):
    state=_make_state(tmp_path); root=_root(state); _agent_resolves(monkeypatch,"default")
    root._session_max_descendants=2; root._session_depth=0; root._session_root_key=root.key
    async def _two():
        return await asyncio.gather(sc.create_session(state, caller_session_key=_key(root)), sc.create_session(state, caller_session_key=_key(root)), return_exceptions=True)
    results=asyncio.run(_two())
    assert len([r for r in results if isinstance(r,dict)])==2
    with pytest.raises(sc.SessionControlError) as e: _create(state, root)
    assert e.value.code=="tree_descendant_quota_exceeded"
