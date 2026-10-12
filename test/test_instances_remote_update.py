"""A connected crew's version skew, and the explicit "Update remote" action.

Two routes and one field:

* ``GET /api/instances/versions`` reads every CONNECTED crew's ``/api/version``
  through the closed-path capability carrier and reports ``behind`` -- the
  exact, pre-release-aware compare against this gateway's build.
* ``behind`` also rides the capabilities reply, so the crew-window header can
  show the badge without a second read.
* ``POST /api/instances/{id}/update`` runs the remote's own ``kirocrew update``
  and then ``kirocrew restart`` as a tracked background job.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew
from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.instances.ssh_tunnel_manager import TunnelState


class _Req:
    """Request stub mirroring aiohttp's mapping surface (see the capabilities tests)."""

    def __init__(self, state, instance_id, identity):
        self.app = {"state": state}
        self.match_info = {"id": instance_id}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, instance_id="nobita", app="", user="local-app"):
    return _Req(state, instance_id, {"user": user, "app": app})


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


class _Registry:
    def __init__(self, names):
        self._rows = [SimpleNamespace(id=iid, name=name) for iid, name in names.items()]

    def list(self):
        return list(self._rows)

    def get(self, iid):
        return next((r for r in self._rows if r.id == iid), None)


def _versions_state(versions, *, states=None, names=None):
    """A manager whose CONNECTED peers answer ``/api/version`` from *versions*.

    A value of ``None`` makes that peer's read fail; an exception instance makes
    it raise.
    """
    states = states or {iid: TunnelState.CONNECTED for iid in versions}
    calls: list[tuple[str, str]] = []

    async def _peer_capability(iid, path):
        calls.append((iid, path))
        outcome = versions.get(iid)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return False, {"code": "capability_unreachable"}
        return True, {"version": outcome}

    mgr = SimpleNamespace(
        peer_capability=_peer_capability,
        status_all=lambda: {iid: SimpleNamespace(state=s) for iid, s in states.items()},
    )
    state = SimpleNamespace(
        instances_manager=mgr,
        instances_registry=_Registry(names or {iid: iid.upper() for iid in states}),
    )
    return state, calls


async def _body(resp):
    return json.loads(resp.body.decode())


class TestRemoteIsBehind:
    @pytest.mark.parametrize(
        ("local", "remote", "behind"),
        [
            ("0.9.1", "0.9.0", True),
            ("0.9.0", "0.9.0rc3", True),
            ("0.9.0rc4", "0.9.0rc3", True),
            ("0.9.0", "0.9.0", False),
            ("0.9.0", "0.9.1", False),
            ("0.9.0", "", False),
            ("0.9.0", "not-a-version", False),
            ("", "0.9.0", False),
        ],
    )
    def test_exact_prerelease_aware_order(self, local, remote, behind):
        assert hi._remote_is_behind(local, remote) is behind


@pytest.mark.asyncio
class TestVersionsRoute:
    async def test_reports_each_connected_crew_and_flags_the_older_one(self, monkeypatch):
        _enable_instances(monkeypatch)
        monkeypatch.setattr(kiro_crew, "__version__", "0.9.1")
        state, calls = _versions_state(
            {"a": "0.9.1", "b": "0.9.0"}, names={"a": "Alpha", "b": "Bravo"}
        )

        resp = await hi.api_instances_versions(_request(state))

        assert resp.status == 200
        data = await _body(resp)
        assert data["local_version"] == "0.9.1"
        assert data["instances"] == [
            {"id": "a", "name": "Alpha", "version": "0.9.1", "behind": False},
            {"id": "b", "name": "Bravo", "version": "0.9.0", "behind": True},
        ]
        assert sorted(calls) == [("a", "/api/version"), ("b", "/api/version")]

    async def test_only_connected_crews_are_read(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state(
            {"a": "0.0.1", "b": "0.0.1"},
            states={"a": TunnelState.CONNECTED, "b": TunnelState.ERROR},
        )

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert [r["id"] for r in data["instances"]] == ["a"]
        assert calls == [("a", "/api/version")]

    async def test_an_unanswered_or_raising_read_is_unknown_never_behind(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": None, "b": ConnectionResetError("tunnel died")})

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert [(r["version"], r["behind"]) for r in data["instances"]] == [
            ("", False),
            ("", False),
        ]

    async def test_a_peer_version_string_is_redacted_and_clamped(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, _ = _versions_state({"a": "9" * 500})

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert len(data["instances"][0]["version"]) == hi._CAP_MAX_VERSION_STR

    async def test_no_manager_answers_an_empty_list(self, monkeypatch):
        _enable_instances(monkeypatch)
        state = SimpleNamespace(instances_manager=None)

        data = await _body(await hi.api_instances_versions(_request(state)))

        assert data == {"local_version": kiro_crew.__version__, "instances": []}

    async def test_an_app_token_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state({"a": "0.0.1"})

        resp = await hi.api_instances_versions(_request(state, app="some-app"))

        assert resp.status == 403
        assert calls == []

    async def test_a_non_owner_dashboard_subject_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)
        state, calls = _versions_state({"a": "0.0.1"})

        resp = await hi.api_instances_versions(_request(state, user="U-someone-else"))

        assert resp.status == 403
        assert calls == []


@pytest.mark.asyncio
class TestCapabilitiesCarryBehind:
    async def _caps(self, monkeypatch, peer_version):
        _enable_instances(monkeypatch)
        monkeypatch.setattr(kiro_crew, "__version__", "0.9.1")

        async def _peer_capability(_iid, path):
            if path == "/api/version":
                return True, {"version": peer_version}
            return True, []

        state = SimpleNamespace(instances_manager=SimpleNamespace(peer_capability=_peer_capability))
        return await _body(await hi.api_instances_capabilities(_request(state)))

    async def test_a_patch_behind_peer_is_compatible_and_behind(self, monkeypatch):
        data = await self._caps(monkeypatch, "0.9.0")
        assert data["version_match"] is True
        assert data["behind"] is True

    async def test_an_equal_peer_is_not_behind(self, monkeypatch):
        data = await self._caps(monkeypatch, "0.9.1")
        assert data["behind"] is False
