"""A resolved static asset that cannot be opened is a 503 + a watchdog nudge,
not a silent 404 (#19463).

When a seccomp filter inherited from a torn-down sandbox turns every ``open``
into ``ENOSYS`` while ``stat`` keeps working, ``_resolve_dist_file`` still finds
the asset (``resolve(strict=True)`` + ``is_file()`` both pass) and
``aiohttp.FileResponse`` then swallows the open failure into a bare 404. The
dist handler now open-probes the resolved path first: a non-``ENOENT`` failure
becomes an explicit no-store 503 and pokes the persistence watchdog, while a
genuine ``ENOENT`` (a healthy filesystem, file swept) stays the 404 it was.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import server as srv


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "main-krfrL6rl.js").write_text("export const x = 1\n", encoding="utf-8")
    return dist


def _make_app(dist: Path) -> web.Application:
    app = web.Application()
    srv._register_dist_static_routes(app, dist)

    @web.middleware  # type: ignore[misc]
    async def no_cache_middleware(request: web.Request, handler: object) -> web.StreamResponse:
        resp = await handler(request)  # type: ignore[operator]
        if hasattr(resp, "headers"):
            srv._apply_security_headers(resp, request.app, request.path, request)
        return resp  # type: ignore[return-value]

    app.middlewares.append(no_cache_middleware)
    srv._install_asset_cache_control_finalizer(app)
    return app


@pytest.mark.asyncio
async def test_enosys_on_open_is_503_and_nudges_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nudges = {"n": 0}
    monkeypatch.setattr(
        srv, "request_persistence_recheck", lambda: nudges.__setitem__("n", nudges["n"] + 1)
    )
    # The asset resolves fine (it exists), but opening it raises ENOSYS — the
    # broken-filesystem state the boot probe cannot catch after boot.
    monkeypatch.setattr(
        srv, "_probe_openable", lambda _p: (errno.ENOSYS, "[Errno 38] Function not implemented")
    )
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/main-krfrL6rl.js")
        assert resp.status == 503, "a resolvable-but-unopenable asset must not be a silent 404"
        cc = resp.headers["Cache-Control"]
        assert "no-store" in cc, cc
        assert "immutable" not in cc, cc
        assert resp.headers.get("Retry-After") == "5"
    assert nudges["n"] == 1, "the persistence watchdog must be nudged to re-probe"


@pytest.mark.asyncio
async def test_eio_on_open_is_also_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "request_persistence_recheck", lambda: None)
    monkeypatch.setattr(srv, "_probe_openable", lambda _p: (errno.EIO, "[Errno 5] I/O error"))
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/main-krfrL6rl.js")
        assert resp.status == 503


@pytest.mark.asyncio
async def test_enoent_on_open_stays_404_and_does_not_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nudges = {"n": 0}
    monkeypatch.setattr(
        srv, "request_persistence_recheck", lambda: nudges.__setitem__("n", nudges["n"] + 1)
    )
    # Resolver found it, but it is genuinely gone by open time on a HEALTHY fs.
    monkeypatch.setattr(
        srv, "_probe_openable", lambda _p: (errno.ENOENT, "[Errno 2] No such file or directory")
    )
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/main-krfrL6rl.js")
        assert resp.status == 404
        assert "no-store" in resp.headers["Cache-Control"]
    assert nudges["n"] == 0, "a healthy-filesystem miss must not nudge the watchdog"


@pytest.mark.asyncio
async def test_healthy_asset_is_served_200(tmp_path: Path) -> None:
    """The open-probe does not break the normal path: a readable asset is 200."""
    async with TestClient(TestServer(_make_app(_dist(tmp_path)))) as client:
        resp = await client.get("/assets/main-krfrL6rl.js")
        assert resp.status == 200
        body = await resp.text()
        assert "export const x" in body


def test_probe_openable_distinguishes_errno(tmp_path: Path) -> None:
    present = tmp_path / "a.js"
    present.write_text("x", encoding="utf-8")
    assert srv._probe_openable(present) is None

    missing = tmp_path / "gone.js"
    result = srv._probe_openable(missing)
    assert result is not None
    assert result[0] == errno.ENOENT
