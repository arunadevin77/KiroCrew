"""``window.kirocrew.subscribe``: the frame contract's third way a value reaches a page.

The two that already exist are a text binding and a frozen snapshot, and neither lets a
page HEAR a change: a binding is filled by the host, and a snapshot is a value read once.
A page that draws a tree, a chart or a scene has to redraw when one field moves, and the
only signal it had was the generic :data:`~kiro_crew.dashboard_frame.PAGE_EVENT` -- which
says "something landed" and leaves the page to diff the whole read itself.

THE BOOTSTRAP IS EXECUTED HERE, not pattern-matched. Every other test of this module
asserts on the JS as a string, which is the right check for a containment rule (``is
textContent used, never innerHTML``) and the wrong one for a behaviour: "fires on first
load and on every push" is a claim about what runs, and a string holding the word
``subscribe`` is satisfied by code that never calls back. So the composed document's
scripts are run under a small DOM shim in Node, and
:func:`test_the_harness_can_see_a_subscriber_that_never_fires` breaks the bootstrap on
purpose and requires the probe to report the silence -- a harness that reports an absence
has to prove first that it can see one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import dashboard_frame

#: The DOM the bootstrap actually touches, and nothing else.
#:
#: Written out rather than taken from a headless browser because the surface under test
#: is four calls wide -- one element lookup, one query, one event registration and one
#: dispatch -- and a real engine would make the failure of this test a question about the
#: engine. The shim is deliberately dumb: it records, it does not interpret.
_SHIM = r"""
const listeners = {};
const dispatched = [];
globalThis.addEventListener = (type, fn) => {
  (listeners[type] = listeners[type] || []).push(fn);
};
globalThis.parent = { postMessage() {} };
globalThis.Event = class { constructor(type) { this.type = type; } };
const nodes = [];
globalThis.document = {
  readyState: 'complete',
  documentElement: { setAttribute() {}, lang: '' },
  getElementById: (id) => (id === 'kirocrew-dashboard-data' ? { textContent: ISLAND } : null),
  querySelectorAll: () => nodes,
};
globalThis.window = globalThis;
globalThis.dispatchEvent = (event) => {
  dispatched.push(event.type);
  for (const fn of listeners[event.type] || []) fn(event);
  return true;
};
globalThis.__push = (read) => {
  for (const fn of listeners.message || []) {
    fn({ source: globalThis.parent, data: { type: DATA_TYPE, read } });
  }
};
globalThis.__calls = [];
"""


def _island(read: dict[str, Any]) -> str:
    """The data island's text, as the composed document carries it."""
    return json.dumps(read, ensure_ascii=False)


def _run(bootstrap: str, read: dict[str, Any], page_js: str) -> list[Any]:
    """Run *bootstrap* then *page_js* over *read*; return what the page recorded.

    *page_js* is the page's own inline script -- the half a template author writes --
    and it pushes onto ``__calls``, so the assertion is about what the PAGE observed
    rather than about the bootstrap's internals.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI builds the website, so node is present
        pytest.skip("node is needed to execute the frame bootstrap")
    program = "\n".join(
        (
            f"const ISLAND = {json.dumps(_island(read))};",
            f"const DATA_TYPE = {json.dumps(dashboard_frame.DATA_MESSAGE_TYPE)};",
            _SHIM,
            bootstrap,
            page_js,
            "console.log(JSON.stringify(globalThis.__calls));",
        )
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


def _read(**over: Any) -> dict[str, Any]:
    fields = over.pop("fields", {"boards": 2, "owner": "atlas"})
    return dashboard_frame.read_payload(fields, **over)


class TestSubscribeOnFirstLoad:
    def test_a_subscriber_is_called_with_the_value_the_island_carries(self) -> None:
        """FIRST LOAD, before any push.

        A page's inline script runs during parse, which is before the host has filled a
        single binding -- so a subscriber that only fired on a push would draw nothing on
        first paint and the reader would see an empty page over a log that has the
        numbers in it. Firing at subscribe time is what makes the callback the page's one
        entry point instead of a thing it needs a binding beside.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == [2]

    def test_a_field_the_read_does_not_carry_fires_nothing(self) -> None:
        """An unresolved field is ABSENT, and absent is not a value.

        Calling back with ``undefined`` would hand the page a number to draw for a fold
        whose path did not resolve, and a page drawing it would show a blank where the
        host's own band says the data is older -- two different statements about one
        cell. The subscriber stays armed and fires when a push carries the field.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"owner": "atlas"}, missing=["boards"]),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == []

    def test_subscribe_survives_the_refill_that_replaces_the_global(self) -> None:
        """The global is replaced wholesale on every push, so ``subscribe`` has to
        travel with it -- a page holding ``window.kirocrew.subscribe`` from first load
        and calling it after a push must not meet ``undefined``."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "__push({ fields: { boards: 9 }, agentic: [], seq: 2, stale: false, missing: [] });"
            "__calls.push(typeof window.kirocrew.subscribe);",
        )
        assert calls == ["function"]


class TestSubscribeOnEveryPush:
    def test_a_push_carrying_the_field_calls_back_with_the_new_value(self) -> None:
        """THE POINT OF THE WHOLE CONTRACT. The host pushes what moved; the page is told
        which field moved and to what, rather than being handed a new snapshot and left
        to diff it."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { boards: 3 }, agentic: [], seq: 2, stale: false, missing: [] });"
            "__push({ fields: { boards: 4 }, agentic: [], seq: 3, stale: false, missing: [] });",
        )
        assert calls == [2, 3, 4]

    def test_a_push_that_does_not_carry_the_field_is_silent_for_it(self) -> None:
        """A subscriber is per FIELD. Firing every subscriber on every push would make
        the callback another spelling of the generic page event, and a page redrawing a
        3D scene on a field it does not read is the cost a reader pays for that."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { owner: 'nyx' }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2]

    def test_one_subscribers_throw_does_not_cost_the_others(self) -> None:
        """Two blocks on one page are two authors' work on a good day and one author's
        on a bad one. A throw in the first must not leave the second holding the value
        from before the push, because that page then shows two fields of different ages
        with nothing saying so."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', () => { throw new Error('x'); });"
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "__push({ fields: { boards: 7 }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2, 7]

    def test_unsubscribing_stops_the_callbacks(self) -> None:
        """``subscribe`` returns its own disposer, so a block that is torn down takes
        its callback with it rather than drawing into a node that is gone."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "const off = window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "off();"
            "__push({ fields: { boards: 5 }, agentic: [], seq: 2, stale: false, missing: [] });",
        )
        assert calls == [2]

    def test_a_push_from_another_frame_calls_nothing_back(self) -> None:
        """The sender check the refill already applies, asserted through the new path.

        Every frame the dashboard embeds runs with ``allow-scripts``, so a sibling can
        reach ``parent.frames[i]`` and post here. A subscriber is a page's most trusted
        input -- it is what the page draws from -- so a value arriving from anything but
        the window that minted the page must not reach one.
        """
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
            "for (const fn of listeners.message || []) {"
            "  fn({ source: { other: true },"
            f"       data: {{ type: {json.dumps(dashboard_frame.DATA_MESSAGE_TYPE)},"
            "         read: { fields: { boards: 66 }, agentic: [], seq: 9,"
            "                 stale: false, missing: [] } } });"
            "}",
        )
        assert calls == [2]


class TestTheBindingsStayTextOnly:
    def test_a_subscribed_value_is_never_written_as_markup(self) -> None:
        """The one containment rule of this module, re-asserted for the field that now
        also reaches a callback: a value can be drawn by the page's own script, and the
        HOST still only ever writes it as a text node.

        ``subscribe`` hands the page a string and what the page does with it is the
        page's business. What must not change is the host's half -- so the bootstrap
        carries no ``innerHTML``, no ``insertAdjacentHTML`` and no ``outerHTML``, in the
        fill path or in the delivery path.
        """
        js = dashboard_frame._BOOTSTRAP_JS
        assert "textContent = text(" in js, "the host no longer fills bindings as text"
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in js, f"the bootstrap reaches {sink}, so a value can become markup"

    def test_a_value_holding_markup_reaches_a_subscriber_as_that_string(self) -> None:
        """A fold value holds whatever the log recorded, including an issue title with a
        tag in it. The callback gets the STRING -- not a parsed node, and not an escaped
        rendering the page would have to undo."""
        calls = _run(
            dashboard_frame._BOOTSTRAP_JS,
            _read(fields={"boards": "<img src=x onerror=alert(1)>"}),
            "window.kirocrew.subscribe('boards', (v) => __calls.push(v));",
        )
        assert calls == ["<img src=x onerror=alert(1)>"]


def test_the_harness_can_see_a_subscriber_that_never_fires() -> None:
    """THE POSITIVE CONTROL for every ``assert calls == []`` above.

    The bootstrap is mutated so delivery is dropped, and this test requires the probe to
    come back empty. Without it, a shim that silently failed to register the page script
    at all would make every "fires" test pass vacuously and every "is silent" test pass
    for the wrong reason -- and the suite would report a contract nothing implements.
    """
    broken = dashboard_frame._BOOTSTRAP_JS.replace("entry.cb(", "(function () {})(")
    assert broken != dashboard_frame._BOOTSTRAP_JS, (
        "the mutation matched nothing, so this control proves nothing: the delivery call "
        "has been renamed and this test has to follow it"
    )
    calls = _run(
        broken,
        _read(),
        "window.kirocrew.subscribe('boards', (v) => __calls.push(v));"
        "__push({ fields: { boards: 3 }, agentic: [], seq: 2, stale: false, missing: [] });",
    )
    assert calls == [], "the harness reports callbacks a bootstrap that cannot deliver made"


def test_the_contract_is_documented_where_a_page_author_reads_it() -> None:
    """A page author reads the module docstring, not the bootstrap. A third way for a
    value to reach a page that is only discoverable by reading minified-looking JS inside
    an f-string is a contract nobody composes against."""
    doc = Path(dashboard_frame.__file__).read_text(encoding="utf-8")
    head = doc[: doc.index('"""', doc.index('"""') + 3)]
    assert "subscribe" in head, (
        "the module docstring lists the ways a value reaches a page and does not mention "
        "subscribe, so the only description of it is the code"
    )
