"""Owners composed by :mod:`kiro_crew.subagent`.

``kiro_crew.subagent`` is split by responsibility across the modules of this package and
stays its only import path and its only patch surface: nothing but ``kiro_crew.subagent``
imports a module of this package.

Each owner holds definitions taken verbatim from the one-module file and keeps no state of
its own. :func:`compose` makes every function an owner defines -- its module functions and
the methods of the classes it defines -- run on ``kiro_crew.subagent``'s module globals
rather than its own. Tests patch ``kiro_crew.subagent.sel``, ``.check_memory_available``,
``.open`` and some ninety more names; a function that read its owner's globals would keep
calling the unpatched object and the test would pass while exercising nothing, so the
subagent module stays ONE namespace, as the one-module file was, and as the
``subagent_manager`` coordinators already are: their ``*_impl`` methods are rebound onto the
same globals by :func:`kiro_crew.subagent_manager.bind_component_globals`, which reaches only
coordinator ``*_impl`` methods and so cannot rebind an owner's functions. Consequences:

* An owner's imports are inert for its functions. They name ``kiro_crew.subagent`` under
  ``TYPE_CHECKING``, for the type checker and the linter; every name a function reads must
  exist in ``kiro_crew.subagent``, and ``test/test_subagent_parts_composition_contract.py``
  sweeps every function's bytecode to prove it does.
* A value an owner computes while it is imported -- a constant, a default argument, a class
  body, a dataclass field factory -- reads the owner's own globals, so the owner imports what
  that needs for real, from the module that defines it.
* A function's ``__module__`` reads ``kiro_crew.subagent``, as before the split; only its
  source file differs. An owner's classes keep their own module, which is where ``inspect``
  looks for a class's source.
* A ``global`` statement in an owner function writes ``kiro_crew.subagent``'s namespace, so
  module state such a function rebinds is read there (``_host_read_in_flight``).

Which responsibility each owner holds, where new code goes, and which constructs stay in
``subagent.py`` because a repository guard reads them there, is recorded in
``docs/system-specs/modules/subagent.md`` (Composition).
"""

from __future__ import annotations

from collections.abc import Iterable
from types import CodeType, FunctionType, ModuleType
from typing import Any


def compose(namespace: dict[str, Any], host: type, owners: Iterable[ModuleType]) -> None:
    """Run every function *owners* define on *namespace*, and bind *host*'s methods.

    *namespace* is the facade's ``globals()`` and *host* the class whose attributes
    bind owner methods. The facade calls this once, after every owner is imported
    and re-exported, so a single pass replaces every binding of an owner function --
    in the facade, in the owners, in the owners' classes and in *host* -- with its
    rebound copy, and no module is left holding the original.

    A function is an owner's when its code was compiled from that owner's file,
    whatever globals it carries: a facade imported a second time into the same
    process rebinds the owners onto its fresh namespace. The owners are imported
    once per process, so their classes are shared by every facade import and
    their methods follow the most recent one.
    """
    owners = tuple(owners)
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
    for cls in [*classes, host]:
        for name, value in list(vars(cls).items()):
            swapped = _swap(value)
            if swapped is not value:
                setattr(cls, name, swapped)

    fresh = {id(new) for _, new in rebound.values()}
    prefix = f"{host.__qualname__}."
    for name, value in vars(host).items():
        for fn in _functions_of(value):
            if id(fn) in fresh:
                fn.__qualname__ = prefix + name
                fn.__code__ = _requalify(fn.__code__, prefix)


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


def _requalify(code: CodeType, prefix: str) -> CodeType:
    """*code* and every code object nested in it, with *prefix* before the qualname.

    Idempotent, so a facade imported a second time does not stack the prefix.
    """
    if code.co_qualname.startswith(prefix):
        return code
    consts = tuple(_requalify(c, prefix) if isinstance(c, CodeType) else c for c in code.co_consts)
    return code.replace(co_qualname=prefix + code.co_qualname, co_consts=consts)
