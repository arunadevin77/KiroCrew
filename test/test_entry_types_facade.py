"""The entry-type registry keeps its content, its order and its one import path.

``kiro_crew.crew_log.entry_types`` composes the session table from five family owners
(``entry_types_lifecycle``, ``entry_types_exchange``, ``entry_types_delegated``,
``entry_types_ledgers``, ``entry_types_surfaces``) written over one leaf,
``entry_types_fields``, and stays the module callers, tests and documents import from.

The registry is a schema: what each declaration says and the order the tables render
in are its contract. So the first tests here hold the composed tables, every lookup and
the rendered reference tables equal to the registry as it stood when it was split into
owners, by digest. A declaration changed on purpose changes its row; the failure prints
the rows to paste. The rest pin the composition: each type and prefix has one owner,
every sibling declaring ``TYPES`` is composed, an owner never imports the facade, every
public name an owner defines other than its ``TYPES`` is the facade's own object, and
every name the test trees
patch through the facade is one the facade itself defines -- an owner's names are read
once, when it is imported, so a patch through the facade could never reach them.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus

import kiro_crew.crew_log as crew_log_package
from kiro_crew.crew_log import entry_types
from kiro_crew.crew_log.schema import KIND_CREW, KIND_SESSION
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_entry_types_facade")

FACADE = "kiro_crew.crew_log.entry_types"
LEAF = "kiro_crew.crew_log.entry_types_fields"
CREW_LOG_DIR = Path(entry_types.__file__).resolve().parent

#: Each kind's declarations when the registry was split into owners: type, then the
#: first 32 hex digits of the SHA-256 of ``_canonical(declaration)``, in table order.
_GOLDEN_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    KIND_SESSION: (
        ("session/opened", "bbda01118211d123f58d2688615b262b"),
        ("session/class", "7524b2522063f07a57b88f8e09af6293"),
        ("session/closed", "a71c964802e62a75e05ac609e252f45e"),
        ("session/unrestored", "8e9b3dfe651250654e3a55429d30c595"),
        ("session/adopted", "525b48ab9bf5d674f4398dcf207417b1"),
        ("session/released", "f1a9ee739bac4c27677df8d0170d2feb"),
        ("session/spawned", "7386772d486639f42c99e15c5e0b2a48"),
        ("turn/started", "4da5599f3789b19155182a2d01cd37da"),
        ("turn/refused", "259f77ef9760e46fb840162b57a263d6"),
        ("turn/completed", "111392f95544c1173c3bc341bd4db8e0"),
        ("write/dropped", "b8b88e298f6dc4bd990b0d3faf1b865e"),
        ("message/received", "cf669c725721589588e02b62e7832828"),
        ("message/sent", "af47be8f0467c00041545fc6be928627"),
        ("message/chunk", "c73c95c95944935a63a56f660df57444"),
        ("message/queued", "6180274d3d30cd59a97b4d0c201a1aed"),
        ("request/configured", "994852b3f44c8bbf3a64e1dd89d1298d"),
        ("context/composed", "62ed63072024699617c932b98cfe6cc7"),
        ("step/started", "fab7519e4fc65ffab0a6b4206e48b580"),
        ("step/completed", "36a041f74224ac4dcfadcc5a861aa0ef"),
        ("tool/called", "065b59afb41bbc227a40882367dfb6f5"),
        ("tool/completed", "5b49060395c2e7ff544a9861fd97f035"),
        ("approval/requested", "69ade2dd5394a25d578f8205938b4179"),
        ("approval/decided", "ef3d9661321ce2f3c680864495c31f1b"),
        ("model/selected", "3b6f12f703b6d53c5e20d98c3afccc27"),
        ("compaction/applied", "59e1ed367b9351f74b6789c68ddec2f6"),
        ("plan/updated", "7cd018ea037a3ceb9a538bf75dc79821"),
        ("subagent/spawned", "6b2c100e7c1ef6bea6a9d24340939362"),
        ("subagent/steered", "cbc89e1578be381a9422777246b13456"),
        ("subagent/dismissed", "cf8c93120ff10fc62430e1d01ff85877"),
        ("subagent/completed", "3d9c7a881ccb9cc2d74d40d131e8d84e"),
        ("subagent/failed", "36bcc7437227a19656db9cfeff419bca"),
        ("background/completed", "c2baf07274afb461f17f4254c09cf3a6"),
        ("ledger/recorded", "ce8a9e0246ca5f62f01fe4d9edaad4b9"),
        ("object/observed", "e7708ecf53c90b1b1e5e5340c59a97e0"),
        ("radar/recorded", "f5da674a52d214bb4b0e46b717ff3042"),
        ("work/recorded", "82f7888c8616c7319b1e65cdb19b8bfe"),
        ("panel/published", "3bed4520ab9f3b183255124dfa140c1e"),
        ("dashboard/instance_changed", "f8187ea96d4c04bc538184bbbbffaca2"),
        ("dashboard/agentic_value", "5fa08587836bf07bca2f89201f0ffc28"),
        ("dashboard/agentic_refused", "8a1ec793a0785956734a05b7783512c8"),
        ("card/proposed", "3aa4b12a8444db31ec2704d9b4206cf1"),
        ("card/finished", "7b65b135a4f6c271df8f31f42152632b"),
        ("guide/offered", "721d8590a703806a77e0e40a10ef2048"),
        ("guide/started", "ebf18278dee992c5eea17a2d0b0341d6"),
        ("guide/finished", "613fdb310da277c35eea97f7911d901b"),
    ),
    KIND_CREW: (
        ("crew/dispatch", "5820b02abe6583c997f2cf40fb1465ff"),
        ("crew/report", "3d913390b8f1b81de87b1ad1cd8abe30"),
    ),
}

#: SHA-256 of ``render_markdown(kind)`` when the registry was split into owners. The
#: member kind declares nothing here, so its digest is of the heading alone.
_GOLDEN_RENDER: dict[str, str] = {
    KIND_SESSION: "4364c860e7415004c2f8de32da13e71c09d2d4cdbfbab1bb34425a3f5b7f0c72",
    KIND_CREW: "ed6c1e44dfba1f48ab44f900b866bf63f6403ee793284a7afa81b3fef970ad9b",
    "member": "5cb4ea02ed8121eb6b285806b4723251aed568df0aa54b9ac8505c9ad389fa8d",
}


def _canonical(spec: entry_types.EntryType) -> str:
    """Everything a declaration states, as one stable string."""
    return json.dumps(dataclasses.asdict(spec), sort_keys=True, separators=(",", ":"))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rows(specs) -> tuple[tuple[str, str], ...]:
    return tuple((spec.type, _digest(_canonical(spec))[:32]) for spec in specs)


def _golden_mismatches(kind: str, specs) -> list[str]:
    """Where *specs* differ from the golden table for *kind*, row by row."""
    got = _rows(specs)
    want = _GOLDEN_TABLES[kind]
    out = [f"{i}: {g} != {w}" for i, (g, w) in enumerate(zip(got, want)) if g != w]
    if len(got) != len(want):
        out.append(f"length {len(got)} != {len(want)}")
    return out


def _paste(kind: str, specs) -> str:
    return "\n".join(f'        ("{t}", "{h}"),' for t, h in _rows(specs)) + f"\n  ({kind})"


def _owner_paths() -> dict[str, Path]:
    return {
        f"kiro_crew.crew_log.{path.stem}": path
        for path in sorted(CREW_LOG_DIR.glob("entry_types_*.py"))
    }


def _top_level_names(tree: ast.Module) -> set[str]:
    """Names a module binds by its own statements, not by its imports."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The tables equal the registry at the split
# --------------------------------------------------------------------------- #


def test_the_session_table_is_its_owners_types_in_owner_order() -> None:
    owners = entry_types._SESSION_OWNERS
    assert [owner.__name__ for owner in owners] == [
        "kiro_crew.crew_log.entry_types_lifecycle",
        "kiro_crew.crew_log.entry_types_exchange",
        "kiro_crew.crew_log.entry_types_delegated",
        "kiro_crew.crew_log.entry_types_ledgers",
        "kiro_crew.crew_log.entry_types_surfaces",
    ]
    composed = [spec for owner in owners for spec in owner.TYPES]
    assert len(composed) == len(entry_types._SESSION_TYPES)
    assert all(a is b for a, b in zip(composed, entry_types._SESSION_TYPES))
    # The table's insertion order IS the render order: render_markdown walks it.
    assert list(entry_types.SESSION_ENTRY_TYPES) == [s.type for s in entry_types._SESSION_TYPES]
    assert all(entry_types.SESSION_ENTRY_TYPES[s.type] is s for s in entry_types._SESSION_TYPES)


@pytest.mark.parametrize("kind", [KIND_SESSION, KIND_CREW])
def test_each_table_equals_the_registry_at_the_split(kind: str) -> None:
    table = entry_types.ENTRY_TYPES[kind]
    assert _golden_mismatches(kind, table.values()) == [], (
        "a declaration differs from the registry at the split; if the change is meant, "
        "replace the rows with:\n" + _paste(kind, table.values())
    )


def test_every_lookup_answers_with_the_tables_own_declaration() -> None:
    assert list(entry_types.ENTRY_TYPES) == [KIND_SESSION, KIND_CREW]
    assert entry_types.ENTRY_TYPES[KIND_SESSION] is entry_types.SESSION_ENTRY_TYPES
    assert entry_types.ENTRY_TYPES[KIND_CREW] is entry_types.CREW_ENTRY_TYPES
    for kind, table in entry_types.ENTRY_TYPES.items():
        for type_name, spec in table.items():
            assert entry_types.declaration_for(kind, type_name) is spec
    assert entry_types.declaration_for("member", "session/opened") is None


@pytest.mark.parametrize("kind", sorted(_GOLDEN_RENDER))
def test_the_reference_tables_render_as_they_did_at_the_split(kind: str) -> None:
    assert _digest(entry_types.render_markdown(kind)) == _GOLDEN_RENDER[kind]
    if kind == KIND_SESSION:
        assert entry_types.render_markdown() == entry_types.render_markdown(KIND_SESSION)


def test_the_golden_check_refuses_a_reordered_or_edited_table() -> None:
    """The comparison above is what fails, not a check that could pass anything."""
    owners = list(entry_types._SESSION_OWNERS)
    owners[1], owners[3] = owners[3], owners[1]
    swapped = [spec for owner in owners for spec in owner.TYPES]
    assert _golden_mismatches(KIND_SESSION, swapped)
    edited = list(entry_types._SESSION_TYPES)
    edited[0] = dataclasses.replace(edited[0], summary=edited[0].summary + " ")
    assert _golden_mismatches(KIND_SESSION, edited) == [
        f"0: {_rows(edited[:1])[0]} != {_GOLDEN_TABLES[KIND_SESSION][0]}"
    ]
    assert _golden_mismatches(KIND_SESSION, entry_types._SESSION_TYPES[:-1])


# --------------------------------------------------------------------------- #
# The owners
# --------------------------------------------------------------------------- #


def test_each_type_and_each_prefix_has_one_owner() -> None:
    owners = entry_types._SESSION_OWNERS
    types = Counter(spec.type for owner in owners for spec in owner.TYPES)
    assert [name for name, count in types.items() if count != 1] == []
    prefixes: dict[str, set[str]] = {}
    for owner in owners:
        assert owner.TYPES, f"{owner.__name__} declares nothing"
        for spec in owner.TYPES:
            prefixes.setdefault(spec.type.split("/")[0], set()).add(owner.__name__)
    assert {p: o for p, o in prefixes.items() if len(o) != 1} == {}
    assert len(prefixes) >= 20
    assert set(entry_types.CREW_ENTRY_TYPES).isdisjoint(entry_types.SESSION_ENTRY_TYPES)


def test_every_sibling_that_declares_types_is_an_owner() -> None:
    declaring = {
        name for name, path in _owner_paths().items() if "TYPES" in _top_level_names(_parsed(path))
    }
    assert declaring == {owner.__name__ for owner in entry_types._SESSION_OWNERS}
    assert LEAF in _owner_paths()
    assert LEAF not in declaring


def _imported_modules(tree: ast.AST, package: str = "kiro_crew.crew_log") -> set[str]:
    """Every module *tree* imports, anywhere in it, with relative imports resolved."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("kiro_crew."):
                found.add(node.value)
    return found


def _imports_the_facade(source: str) -> bool:
    return FACADE in _imported_modules(ast.parse(source))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import kiro_crew.crew_log.entry_types\n", True),
        ("from kiro_crew.crew_log import entry_types\n", True),
        ("from kiro_crew.crew_log.entry_types import Field\n", True),
        ("from . import entry_types\n", True),
        ("from .entry_types import Field\n", True),
        (
            "def f():\n    import importlib\n    importlib.import_module('kiro_crew.crew_log.entry_types')\n",
            True,
        ),
        ("from kiro_crew.crew_log.entry_types_fields import Field\n", False),
        ("from kiro_crew.crew_log import entry_types_fields\n", False),
        ("x = 'kiro_crew.crew_log.entry_types_ledgers'\n", False),
    ],
)
def test_the_facade_import_detector_answers_both_ways(source: str, expected: bool) -> None:
    assert _imports_the_facade(source) is expected


def test_owners_never_import_the_facade() -> None:
    paths = _owner_paths()
    assert len(paths) >= 6
    assert sorted(n for n, p in paths.items() if _imports_the_facade(p.read_text("utf-8"))) == []


def test_the_leaf_imports_nothing_from_the_project_and_families_import_no_family() -> None:
    paths = _owner_paths()
    leaf_imports = _imported_modules(_parsed(paths[LEAF]))
    assert sorted(m for m in leaf_imports if m.startswith("kiro_crew")) == []
    for name, path in paths.items():
        imported = {m for m in _imported_modules(_parsed(path)) if m in paths or m == FACADE}
        assert imported <= ({LEAF} - {name}), (name, imported)


def test_importing_every_owner_leaves_the_facade_unloaded(tmp_path: Path) -> None:
    """Checked in a fresh interpreter: an import cycle through a producer module (the
    ledger, the work vocabulary, the omission kinds) is invisible to an AST scan."""
    probe = (
        "import importlib, json, sys\n"
        f"for name in {sorted(_owner_paths())!r}:\n"
        "    importlib.import_module(name)\n"
        f"print(json.dumps({FACADE!r} in sys.modules))\n"
    )
    env = dict(os.environ)
    src = str(Path(entry_types.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) is False


# --------------------------------------------------------------------------- #
# The facade's surface
# --------------------------------------------------------------------------- #


def _owner_defined() -> dict[str, set[str]]:
    return {name: _top_level_names(_parsed(path)) for name, path in _owner_paths().items()}


def test_every_public_name_an_owner_defines_is_the_facades_own_object() -> None:
    seen: dict[str, str] = {}
    for name, defined in _owner_defined().items():
        owner = importlib.import_module(name)
        for attr in sorted(defined):
            if attr.startswith("_") or attr == "TYPES":
                continue
            assert attr not in seen, f"{attr} is defined by {seen[attr]} and {name}"
            seen[attr] = name
            assert getattr(entry_types, attr, None) is getattr(owner, attr), (name, attr)
    facade_defined = {n for n in _top_level_names(_parsed(Path(entry_types.__file__)))}
    assert sorted(facade_defined & set(seen)) == []
    assert len(seen) >= 50


def test_the_package_serves_the_facades_objects() -> None:
    forwarded = [n for n, owner in crew_log_package._EXPORTS.items() if owner == "entry_types"]
    assert {"Field", "EntryType", "SESSION_ENTRY_TYPES"} <= set(forwarded)
    for name in forwarded:
        assert getattr(crew_log_package, name) is getattr(entry_types, name), name


# --------------------------------------------------------------------------- #
# Patch reach
# --------------------------------------------------------------------------- #


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _receivers(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Local names bound to the facade, and to the ``kiro_crew.crew_log`` package."""
    facade, package = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if full == FACADE:
                    facade.add(alias.asname or alias.name)
                elif full == "kiro_crew.crew_log":
                    package.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FACADE:
                    facade.add(alias.asname or FACADE)
                elif alias.name == "kiro_crew.crew_log":
                    package.add(alias.asname or "kiro_crew.crew_log")
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                _dotted(call.func) in {"importlib.import_module", "import_module"}
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in (FACADE, "kiro_crew.crew_log")
            ):
                into = facade if call.args[0].value == FACADE else package
                into.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return facade, package


def patched_names(source: str, forwarded: frozenset[str]) -> set[str]:
    """Every name *source* patches on the facade, directly or through the package names
    the package forwards to it, by any spelling a test uses."""
    tree = ast.parse(source)
    facade, package = _receivers(tree)

    def through(receiver: ast.expr, name: str) -> bool:
        dotted = _dotted(receiver)
        if dotted is None:
            return False
        if dotted in facade or dotted == FACADE:
            return True
        head, _, last = dotted.rpartition(".")
        if last == "entry_types" and (head in package or head == "kiro_crew.crew_log"):
            return True
        return (dotted in package or dotted == "kiro_crew.crew_log") and name in forwarded

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and through(target.value, target.attr):
                    found.add(target.attr)
            continue
        if not isinstance(node, ast.Call):
            continue
        leaf = (_dotted(node.func) or "").rpartition(".")[2]
        args = node.args
        target = args[0] if args else None
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            # A string target names the module, and for these three the attribute too.
            if leaf in {"setattr", "delattr", "patch"}:
                head, _, name = target.value.rpartition(".")
                if head == FACADE or (head == "kiro_crew.crew_log" and name in forwarded):
                    found.add(name)
            elif leaf == "multiple" and target.value in (FACADE, "kiro_crew.crew_log"):
                found.update(
                    k.arg
                    for k in node.keywords
                    if k.arg and (target.value == FACADE or k.arg in forwarded)
                )
            continue
        if leaf in {"setattr", "delattr", "object"} and len(args) >= 2:
            name = args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                if through(args[0], name.value):
                    found.add(name.value)
            continue
        if leaf == "multiple" and args:
            found.update(k.arg for k in node.keywords if k.arg and through(args[0], k.arg))
    return found


_FORWARDED = frozenset(
    n for n, owner in crew_log_package._EXPORTS.items() if owner == "entry_types"
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew.crew_log import entry_types\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(entry_types, 'A', 1)\n",
            {"A"},
        ),
        (
            "from kiro_crew.crew_log import entry_types as reg\n"
            "def t(monkeypatch):\n    monkeypatch.delattr(reg, 'B')\n",
            {"B"},
        ),
        (
            "import kiro_crew.crew_log.entry_types as et\n"
            "from unittest import mock\n"
            "def t():\n    with mock.patch.object(et, 'C'):\n        pass\n",
            {"C"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.entry_types.D', 1)\n",
            {"D"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.crew_log.entry_types.E'):\n        pass\n",
            {"E"},
        ),
        (
            "import importlib\n"
            "M = importlib.import_module('kiro_crew.crew_log.entry_types')\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(M, 'F', 1)\n",
            {"F"},
        ),
        (
            "from kiro_crew.crew_log import entry_types\n"
            "from unittest import mock\n"
            "def t():\n    with mock.patch.multiple(entry_types, G=1):\n        pass\n",
            {"G"},
        ),
        (
            "from kiro_crew.crew_log import entry_types as reg\n"
            "def t():\n    reg.H = 1\n    del reg.I\n",
            {"H", "I"},
        ),
        (
            "import kiro_crew.crew_log as pkg\n"
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr(pkg, 'Field', 1)\n"
            "    monkeypatch.setattr(pkg, 'CrewLog', 1)\n",
            {"Field"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.validate_data', 1)\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.CrewLog', 1)\n",
            {"validate_data"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.entry_types.M', 'm/x')\n"
            "    monkeypatch.delattr('kiro_crew.crew_log.entry_types.N', raising=False)\n",
            {"M", "N"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.declaration_for', 'v')\n",
            {"declaration_for"},
        ),
        (
            "from unittest import mock\n"
            "def t():\n"
            "    with mock.patch.multiple('kiro_crew.crew_log.entry_types', O=1):\n"
            "        pass\n"
            "    with mock.patch.multiple('kiro_crew.crew_log', Field=1, CrewLog=1):\n"
            "        pass\n",
            {"O", "Field"},
        ),
        (
            "from kiro_crew import crew_log as lg\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(lg.entry_types, 'P', ())\n",
            {"P"},
        ),
        (
            "import kiro_crew.crew_log.store\n"
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr(kiro_crew.crew_log.entry_types, 'Q', ())\n"
            "    monkeypatch.setattr(kiro_crew.crew_log, 'render_markdown', 1)\n",
            {"Q", "render_markdown"},
        ),
        # Another module's patch, a dict item, and a name chosen at run time.
        (
            "from kiro_crew.crew_log import store\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(store, 'X', 1)\n",
            set(),
        ),
        (
            "from kiro_crew.crew_log.entry_types import ENTRY_TYPES\n"
            "def t(monkeypatch):\n    monkeypatch.setitem(ENTRY_TYPES, 'crew', {})\n",
            set(),
        ),
        (
            "from kiro_crew.crew_log import entry_types\n"
            "def t(monkeypatch, name):\n    monkeypatch.setattr(entry_types, name, 1)\n",
            set(),
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.crew_log.entry_types_ledgers.Y', 1)\n",
            set(),
        ),
    ],
    ids=[
        "monkeypatch-alias",
        "delattr-renamed-alias",
        "patch-object-import-as",
        "dotted-setattr",
        "mock-patch-string",
        "import-module",
        "patch-multiple",
        "plain-assignment-and-del",
        "package-object-forwarded",
        "package-string-forwarded",
        "dotted-string-with-string-value",
        "package-string-with-string-value",
        "patch-multiple-string-target",
        "package-alias-attribute-receiver",
        "full-dotted-receiver",
        "other-module",
        "dict-item",
        "name-chosen-at-run-time",
        "owner-module-string",
    ],
)
def test_the_patch_scan_finds_every_spelling(source: str, expected: set[str]) -> None:
    assert patched_names(source, _FORWARDED) == expected


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> tuple[frozenset[str], int]:
    """Every name the test trees patch through the facade, and how many files were read."""
    found: set[str] = set()
    read = 0
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "crew_log" not in text:
            continue
        read += 1
        found |= patched_names(text, _FORWARDED)
    return frozenset(found), read


def test_every_name_the_tests_patch_through_the_facade_is_the_facades_own() -> None:
    """A name an owner defines is read once, when the owner is imported, so a patch of
    it through the facade could never reach the declaration that read it. Patch the
    facade's own names (the lookup, the validator, the renderer) or build the data."""
    found, read = _corpus_patched_names()
    assert read >= 20, "the scan read almost nothing: the corpus filter stopped matching"
    owner_defined = set().union(*_owner_defined().values())
    assert sorted(found & owner_defined) == []


def test_the_owners_are_plain_modules() -> None:
    """The facade is a plain module and so is every owner: a read is an attribute
    lookup and a write binds the one module written to."""
    for owner in (entry_types, *entry_types._SESSION_OWNERS):
        assert type(owner) is ModuleType, owner.__name__
