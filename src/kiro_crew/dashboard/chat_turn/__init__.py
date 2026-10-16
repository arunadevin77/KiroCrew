"""Private owners composed by :mod:`kiro_crew.dashboard.chat_runner`.

The dashboard chat runner is split by responsibility across the modules of this
package, and ``kiro_crew.dashboard.chat_runner`` stays its only import path and
its only patch surface: nothing but the runner imports an owner.

Each owner holds the runner's top-level helpers for its responsibility, moved
verbatim, and the phases of a dashboard turn: functions cut from ``_run_chat`` and
``_start_next_queued_turn`` whose bodies keep the original statements. The phases
that exist take the locals they read as parameters and return what they change; a
phase newly cut from ``_run_chat`` takes the turn's :class:`turn_locals._ChatTurn`
instead and reads the state the turn shares off it. :func:`compose` binds on the
runner every name an owner defines, so ``chat_runner.<name>`` resolves for each, and
makes every function an owner defines -- its module functions and the methods of the
classes it defines -- run on the runner's module globals rather than its own. Tests
patch the runner's names (``chat_runner.sel``, ``.save_slot_off_loop``,
``._flush_file_changes`` and about a hundred more). A function that read its owner's
globals would keep calling the unpatched object and the test would pass while
exercising nothing, so the runner stays ONE namespace, as the one-module file was.
Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the runner and
  its siblings only under ``TYPE_CHECKING``; stdlib names, and the project constants
  the module evaluates when it loads (``steer_queue`` from ``chat_utils``,
  ``tool_approval`` from ``deny_guidance``), are imported plainly. Both serve the
  type checker and the linter, and what the module itself evaluates when it loads
  (a default argument, a class base, a constant). Every name a function reads must
  be bound in ``kiro_crew.dashboard.chat_runner`` -- by the runner's own imports, which
  is why the runner keeps imports only its owners read, or by :func:`compose`, which
  binds each owner's own names there -- and
  ``test_chat_runner_composition_contract.py`` sweeps each function's bytecode to
  prove it does. A name bound on the runner and in an owner is the SAME object, or
  the runner fails to import.
* A function's ``__module__`` reads ``kiro_crew.dashboard.chat_runner`` as it did
  before the split, so reprs and pickling by reference are unchanged, and it logs
  through the runner's ``logger``. Its source file differs, so a log record's
  ``module``, ``filename`` and ``lineno`` name the owner file. An owner's classes
  keep their own module, which is where ``inspect`` looks for a class's source.
* State the runner owns stays on the runner. A module-level mutable (a registry,
  a cache, a semaphore) is a runner global that owner functions reach by name
  through the runner's namespace, so a test that rebinds it on the runner is the
  one every function sees.

The technique is the one the shared ``kiro_crew._facade_compose.compose`` applies to
every other split facade. The runner keeps its own copy because it also finds its
owners (:func:`owner_modules`) and binds their names on the runner, which the shared
helper does not do.

Which responsibility each owner holds, and which constructs stay in the runner
because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (Chat runner composition).
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterable
from types import FunctionType, ModuleType
from typing import Any


def owner_modules() -> tuple[ModuleType, ...]:
    """Every owner module of this package, in name order: the one already imported
    if there is one, else imported now. A file whose name is not an identifier is
    no module of the package and is skipped."""
    owners = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda info: info.name):
        if not info.name.isidentifier():
            continue
        name = f"{__name__}.{info.name}"
        owners.append(sys.modules.get(name) or importlib.import_module(name))
    return tuple(owners)


def compose(namespace: dict[str, Any], owners: Iterable[ModuleType]) -> None:
    """Bind on *namespace* what *owners* define, and run every function they define on it.

    *namespace* is the runner's ``globals()``. The runner calls this once, after
    its own body has bound every name. First every name an owner binds that the
    runner lacks is bound on it (:func:`_bind`), so ``chat_runner.<name>`` resolves
    for every owner's function, class and constant without a by-name re-export;
    then a single pass replaces every binding of an owner function -- in the runner,
    in the owners and in the owners' classes -- with its rebound copy, and no module
    is left holding the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a runner imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every runner import and their
    methods follow the most recent one.
    """
    owners = tuple(owners)
    _bind(namespace, owners)
    rebound: dict[int, tuple[FunctionType, FunctionType]] = {}
    classes: list[type] = []
    for owner in owners:
        path = owner.__file__
        for value in list(vars(owner).values()):
            if isinstance(value, FunctionType) and value.__code__.co_filename == path:
                rebound[id(value)] = (value, _rebind(value, namespace))
            elif isinstance(value, type) and value.__module__ == owner.__name__:
                classes.append(value)
                for member in vars(value).values():
                    for fn in _functions_of(member):
                        if fn.__code__.co_filename == path:
                            rebound[id(fn)] = (fn, _rebind(fn, namespace))

    def _swap(value: Any) -> Any:
        if isinstance(value, FunctionType):
            pair = rebound.get(id(value))
            return pair[1] if pair is not None and pair[0] is value else value
        if isinstance(value, (staticmethod, classmethod)):
            fn = _swap(value.__func__)
            return value if fn is value.__func__ else type(value)(fn)
        if isinstance(value, property):
            parts = (_swap(value.fget), _swap(value.fset), _swap(value.fdel))
            if parts == (value.fget, value.fset, value.fdel):
                return value
            return property(*parts, value.__doc__)
        return value

    for holder in [namespace, *(vars(owner) for owner in owners)]:
        for name, value in list(holder.items()):
            swapped = _swap(value)
            if swapped is not value:
                holder[name] = swapped
    for cls in classes:
        for name, value in list(vars(cls).items()):
            swapped = _swap(value)
            if swapped is not value:
                setattr(cls, name, swapped)


def _bind(namespace: dict[str, Any], owners: tuple[ModuleType, ...]) -> None:
    """Bind on *namespace* every name an owner binds at runtime that *namespace* lacks.

    A module object is left out (the runner imports the modules its owners read) and
    so are dunder names and ``TYPE_CHECKING``. A name both already bind must be the
    same object: owner functions read the runner's binding, so a different object
    there would be a different value from the one the owner's own code evaluated,
    and the import fails rather than run with two.
    """
    for owner in owners:
        for name, value in vars(owner).items():
            if name.startswith("__") or name == "TYPE_CHECKING" or isinstance(value, ModuleType):
                continue
            held = namespace.get(name, _ABSENT)
            if held is _ABSENT:
                namespace[name] = value
            elif held is not value:
                raise ImportError(
                    f"{owner.__name__}.{name} and {namespace.get('__name__')}.{name} are "
                    "different objects; one namespace cannot hold both"
                )


_ABSENT = object()


def _functions_of(member: Any) -> list[FunctionType]:
    """The plain functions a class member wraps: itself, a static/class method, a property."""
    if isinstance(member, FunctionType):
        return [member]
    if isinstance(member, (staticmethod, classmethod)) and isinstance(
        member.__func__, FunctionType
    ):
        return [member.__func__]
    if isinstance(member, property):
        return [
            fn for fn in (member.fget, member.fset, member.fdel) if isinstance(fn, FunctionType)
        ]
    return []


def _rebind(fn: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    """A copy of *fn* whose globals are *namespace*; every other attribute kept."""
    new = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    new.__kwdefaults__ = fn.__kwdefaults__
    new.__annotations__ = fn.__annotations__
    new.__dict__.update(fn.__dict__)
    new.__doc__ = fn.__doc__
    new.__qualname__ = fn.__qualname__
    new.__module__ = namespace["__name__"]
    new.__type_params__ = fn.__type_params__
    return new
