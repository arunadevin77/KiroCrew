"""The per-test timeout a ``tree_scan`` group carries, and the two marks that escape it.

The ``tree_scan_*`` xdist groups hold the gates that parse or walk a whole tree: the
package under ``src/kiro_crew`` or the suite under ``test/``. The slow ones take
20-42 s of CPU on a fast host and about three times that on a hosted runner, which
is past the suite-wide ``--timeout=120``.

``test/conftest.py`` gives every test in a ``tree_scan`` group that carries no
timeout value of its own the group default, :data:`conftest.TREE_SCAN_TIMEOUT_SECS`.
This module pins that hook, and fails on the two marks that would put a test in a
``tree_scan`` module back under a shorter timeout:

* a ``tree_scan`` group on a class or a function instead of the module, which
  leaves the module's other tests at 120 s;
* an explicit timeout below the default in a ``tree_scan`` module, outside a test
  that carries a group of its own.

It reads a timeout's value from a literal, a module-level int constant (plain or
annotated) or arithmetic over those; any other value in a grouped module (an imported
constant, a call) is reported, with a request for a literal or a module constant. It
does not see these spellings, none of which the suite uses: a mark taken by
``from pytest import mark``, a mark held in a variable, an annotated or ``+=``
``pytestmark``, and a group name taken from a constant.

It does not find scans. A slow scan in a module with no ``tree_scan`` group still
runs at 120 s, and nothing here sees it. The fix for that is to narrow the scan
through ``test/source_corpus.py`` (``parsed_candidates`` with the literals a hit
must spell), so only the files that can match are parsed. Where no literal can
narrow it, the module takes a ``tree_scan`` group; see
``docs/system-specs/common/testing-conventions.md``.
"""

from __future__ import annotations

import ast
import re

import pytest
from source_corpus import repo_files_named, repo_root

import conftest
from conftest import TREE_SCAN_TIMEOUT_SECS

# ``pytester`` is not registered by default; a test module may request it by name.
pytest_plugins = ("pytester",)

pytestmark = pytest.mark.xdist_group(name="tree_scan_tree_scan_budget")

REPO_ROOT = repo_root()


def _marks(value: ast.AST | None, attr: str) -> list[ast.Call]:
    """Each ``pytest.mark.<attr>(...)`` call in a decorator or a ``pytestmark`` value."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return [call for item in value.elts for call in _marks(item, attr)]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == attr
        and isinstance(value.func.value, ast.Attribute)
        and value.func.value.attr == "mark"
    ):
        return [value]
    return []


def _is_tree_scan_group(call: ast.Call) -> bool:
    name = next((kw.value for kw in call.keywords if kw.arg == "name"), None)
    if name is None and call.args:
        name = call.args[0]
    return (
        isinstance(name, ast.Constant)
        and isinstance(name.value, str)
        and name.value.startswith("tree_scan")
    )


def _pytestmark(body: list[ast.stmt]) -> list[ast.AST]:
    return [
        stmt.value
        for stmt in body
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets)
    ]


_ARITHMETIC = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
}


def _seconds(node: ast.AST, numbers: dict[str, float]) -> float | None:
    """The number *node* spells from literals, *numbers* and arithmetic; None if unreadable."""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return None
    if isinstance(node, ast.Name):
        return numbers.get(node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC:
        left, right = _seconds(node.left, numbers), _seconds(node.right, numbers)
        if left is None or right is None:
            return None
        try:
            return _ARITHMETIC[type(node.op)](left, right)
        except ZeroDivisionError:
            return None
    return None


def _module_numbers(tree: ast.Module) -> dict[str, float]:
    """Module-level ``NAME = <number>`` and ``NAME: int = <number>``, read in order."""
    numbers: dict[str, float] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        seconds = _seconds(value, numbers)
        for target in targets:
            if isinstance(target, ast.Name):
                if seconds is None:
                    numbers.pop(target.id, None)
                else:
                    numbers[target.id] = seconds
    return numbers


def problems_in(rel: str, source: str) -> list[str]:
    """The ``tree_scan`` marks in one test module that escape the group default."""
    tree = ast.parse(source)
    numbers = _module_numbers(tree)
    module_marks = _pytestmark(tree.body)
    grouped = any(_is_tree_scan_group(c) for v in module_marks for c in _marks(v, "xdist_group"))
    found: list[str] = []
    timeouts = [c for v in module_marks for c in _marks(v, "timeout")]

    def visit(body: list[ast.stmt], own_group: bool) -> None:
        # *own_group*: an enclosing class names a group that is not tree_scan, so the
        # hook leaves its tests at their own budget.
        for node in body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            marks: list[ast.AST] = list(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                marks += _pytestmark(node.body)
            groups = [c for value in marks for c in _marks(value, "xdist_group")]
            for call in groups:
                if _is_tree_scan_group(call):
                    found.append(
                        f"{rel}:{node.lineno} {node.name}: a tree_scan group below module level"
                    )
            inner = own_group or any(not _is_tree_scan_group(c) for c in groups)
            if not inner:
                timeouts.extend(c for value in marks for c in _marks(value, "timeout"))
            visit(node.body, inner)

    visit(tree.body, False)
    if not grouped:
        return found
    for call in timeouts:
        keyword = [kw.value for kw in call.keywords if kw.arg == "timeout"]
        value = call.args[0] if call.args else keyword[0] if keyword else None
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            continue
        seconds = _seconds(value, numbers)
        if seconds is None:
            found.append(
                f"{rel}:{call.lineno}: cannot read the timeout value; "
                "use a literal or a module constant"
            )
        elif seconds < TREE_SCAN_TIMEOUT_SECS:
            found.append(f"{rel}:{call.lineno}: timeout({seconds}) is below the tree_scan default")
    return found


def test_no_tree_scan_mark_escapes_the_group_default() -> None:
    """Every ``tree_scan`` group is module level, and no test in one runs under the default."""
    found: list[str] = []
    for path in repo_files_named(".py"):
        source = path.read_text(encoding="utf-8")
        if "tree_scan" in source:
            found += problems_in(path.relative_to(REPO_ROOT).as_posix(), source)
    assert not found, "\n".join(found)


_PLANTED_ESCAPES = """
import pytest
from elsewhere import IMPORTED

SHORT = 60
N = 2
ANNOTATED: int = 60
LONG: int = 10 * 60
pytestmark = pytest.mark.xdist_group(name="tree_scan_planted")

@pytest.mark.timeout(60)
def test_short():
    pass

@pytest.mark.timeout(timeout=SHORT)
def test_keyword_short():
    pass

class TestGrouped:
    pytestmark = pytest.mark.xdist_group(name="tree_scan_planted_class")

@pytest.mark.xdist_group("tree_scan_planted_function")
def test_function_group():
    pass

@pytest.mark.timeout(400)
def test_budgeted():
    pass

@pytest.mark.timeout(method="thread")
def test_method_only():
    pass

@pytest.mark.timeout(N * 30)
def test_computed_short():
    pass

@pytest.mark.timeout(ANNOTATED)
def test_annotated_short():
    pass

@pytest.mark.timeout(IMPORTED)
def test_imported():
    pass

@pytest.mark.timeout(10 * 60)
def test_computed_long():
    pass

@pytest.mark.timeout(LONG)
def test_annotated_long():
    pass

@pytest.mark.xdist_group(name="serial")
@pytest.mark.timeout(60)
def test_own_group():
    pass

@pytest.mark.xdist_group(name="serial")
class TestOwnGroup:
    @pytest.mark.timeout(60)
    def test_short_in_its_own_group(self):
        pass
"""

_PLANTED_CLASS_GROUP = """
import pytest

@pytest.mark.xdist_group(name="tree_scan_planted")
class TestScan:
    @pytest.mark.timeout(60)
    def test_short_outside_a_grouped_module(self):
        pass
"""


def test_the_check_fails_each_escape_and_passes_the_rest() -> None:
    """Planted: a sub-default timeout by value, name or arithmetic, an unreadable value,
    and a group below module level. A test in a group of its own is not reported."""
    found = problems_in("planted.py", _PLANTED_ESCAPES)
    assert [line.split(":", 1)[1] for line in found] == [
        "19 TestGrouped: a tree_scan group below module level",
        "23 test_function_group: a tree_scan group below module level",
        "11: timeout(60) is below the tree_scan default",
        "15: timeout(60) is below the tree_scan default",
        "34: timeout(60) is below the tree_scan default",
        "38: timeout(60) is below the tree_scan default",
        "42: cannot read the timeout value; use a literal or a module constant",
    ], found
    # In a module with no group of its own only the class group is reported: moving
    # it to the module is the fix, and that move is what puts the timeout under check.
    assert problems_in("class.py", _PLANTED_CLASS_GROUP) == [
        "class.py:5 TestScan: a tree_scan group below module level"
    ]


def test_the_group_default_is_over_the_suite_timeout() -> None:
    """The default lifts tree scans out of the suite-wide ``--timeout`` in setup.cfg."""
    found = re.findall(r"--timeout=(\d+)", (REPO_ROOT / "setup.cfg").read_text(encoding="utf-8"))
    assert found, "setup.cfg no longer sets a suite-wide --timeout"
    assert all(TREE_SCAN_TIMEOUT_SECS > int(value) for value in found)


def test_conftest_gives_a_tree_scan_test_the_group_default(request: pytest.FixtureRequest) -> None:
    """This module is in a tree_scan group with no timeout mark, so it runs at the default."""
    marker = request.node.get_closest_marker("timeout")
    assert marker is not None and marker.args == (TREE_SCAN_TIMEOUT_SECS,)


_HOOKED = """
import pytest
pytestmark = pytest.mark.xdist_group(name="tree_scan_hooked")

def test_plain():
    pass

@pytest.mark.timeout(method="thread")
def test_method_only():
    pass

@pytest.mark.timeout(None, "thread")
def test_positional_none():
    pass

@pytest.mark.timeout(timeout=60)
def test_keyword():
    pass

@pytest.mark.timeout(400)
def test_explicit():
    pass

@pytest.mark.xdist_group(name="serial")
def test_other_group():
    pass
"""

#: Module-level budgets: a test with no mark, or a mark with no value, keeps the module's.
_MODULE_BUDGET = """
import pytest
pytestmark = [pytest.mark.xdist_group(name="tree_scan_budgeted"), pytest.mark.timeout(600)]

def test_module_budget():
    pass

@pytest.mark.timeout(method="thread")
def test_valueless_under_a_module_budget():
    pass
"""

_UNDER_A_SHORT_MODULE = """
import pytest
pytestmark = [pytest.mark.xdist_group(name="tree_scan_short"), pytest.mark.timeout(60)]

@pytest.mark.timeout(400)
def test_function_budget():
    pass
"""


def _effective_timeouts(pytester: pytest.Pytester, source: str) -> dict[str, tuple[object, object]]:
    """(timeout, method) pytest-timeout reads for each item of *source*, after the hook."""
    import pytest_timeout

    # Root the inner session at its own directory so the repository's conftest and
    # setup.cfg are not loaded into it (see test_pytest_asyncio_scan_gate.py).
    pytester.makeini("[pytest]\n")
    items = pytester.getitems(source)
    conftest.pytest_collection_modifyitems(items[0].config, items)
    out: dict[str, tuple[object, object]] = {}
    for item in items:
        settings = pytest_timeout._get_item_settings(item)
        out[item.name] = (settings.timeout, settings.method)
    return out


def test_the_hook_gives_a_valueless_timeout_mark_the_default(pytester: pytest.Pytester) -> None:
    """A mark naming no seconds gets the default and keeps its method; a value is kept."""
    got = _effective_timeouts(pytester, _HOOKED)
    default = float(TREE_SCAN_TIMEOUT_SECS)
    assert got["test_plain"][0] == default
    assert got["test_method_only"] == (default, "thread")
    assert got["test_positional_none"] == (default, "thread")
    assert got["test_keyword"][0] == 60.0
    assert got["test_explicit"][0] == 400.0
    # Outside the tree_scan group the hook adds nothing.
    assert got["test_other_group"][0] is None


def test_the_hook_keeps_a_module_budget(pytester: pytest.Pytester) -> None:
    """A module-level budget is not overridden by the default, under a valueless mark too:
    the closest explicit value at any level wins over the default."""
    got = _effective_timeouts(pytester, _MODULE_BUDGET)
    assert got["test_module_budget"][0] == 600.0
    assert got["test_valueless_under_a_module_budget"] == (600.0, "thread")


def test_the_hook_keeps_a_function_budget_over_a_module_one(pytester: pytest.Pytester) -> None:
    """A closer function budget wins over a shorter module one."""
    got = _effective_timeouts(pytester, _UNDER_A_SHORT_MODULE)
    assert got["test_function_budget"][0] == 400.0
