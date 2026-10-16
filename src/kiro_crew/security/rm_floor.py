"""Recursive-force ``rm`` floor behind the two ``local-destructive-rm-rf-*`` rows.

The catalog patterns ``rm -rf /.*`` and ``rm -rf ~.*`` match one spelling; a shell
runs the same wipe for ``rm -fr /``, ``rm -r -f /``, ``rm --recursive --force /`` and
``rm -rf $HOME``. This floor reads each ``rm`` argv for recursive and force in any
packing, order or long form, against the root or the home directory ITSELF.

It writes no quote reader of its own. Commands and words are cut on the shared
quote state machine (``_iter_shell_chars``) and each word is unquoted with the
shared ``_shell_tokens``, which expands nothing. Home is read off the RAW word, so
``'~'``, ``\\~`` and ``$home`` stay literal. Quoted shell payloads (``bash -c
'...'``), descendants, exec wrappers (``sudo``), parameter-expansion defaults and
interpreter bodies stay with the regex rows and the OS sandbox.
"""

from __future__ import annotations

import os
import posixpath
import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

#: The catalog row each wipe target maps to. The floor is gated on that row's
#: pattern, so an operator who disables the row disables the floor with it.
_RM_FLOOR_RULE_IDS: dict[str, str] = {
    "root": "local-destructive-rm-rf-root",
    "home": "local-destructive-rm-rf-home",
}
_RM_FLOOR_BY_KIND: dict[str, tuple[str, str]] = {
    kind: (rule.id, rule.pattern)
    for kind, rule_id in _RM_FLOOR_RULE_IDS.items()
    for rule in BUILTIN_DENIED_RULES
    if rule.id == rule_id
}
_RM_FLOOR_NOTE = (
    "Matched structurally on the command's argv, not by the pattern text above: "
    "rm with recursive and force flags, in any spelling, targets this directory itself."
)
_RM_FLOOR_UNREAD_NOTE = (
    "Refused because the rm floor could not read this command, so it cannot rule "
    "out a wipe of this directory. Simplify it."
)

#: Cheap necessary condition: the program is a literal ``rm`` (quote splices
#: allowed) or is built by ``$`` / ``\\`` / a backtick.
_CAN_FIRE_RE = re.compile(r"r[^\w\s]*m|[$\\`]")
#: A redirection glued to its target at the program position (``2>/dev/null rm``).
_LEADING_REDIRECT_RE = re.compile(r"^\d*(?:[<>]|>>|&>)&?\S+$")
#: Unquoted characters that end a simple command.
_CUT_CHARS = frozenset(";&|\n()`")
#: Reserved words that keep the next word at command position.
_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
#: The raw start of a home word: an unquoted ``~``, or ``$HOME`` / ``${HOME}``
#: bare or in double quotes. Case-sensitive, as bash is.
_HOME_RAW_RE = re.compile(r'~|"?\$(?:HOME(?!\w)|\{HOME\})')
#: A heredoc tag after ``<<`` / ``<<-``: one word, quotes allowed.
_HEREDOC_TAG_RE = re.compile(r"<<(-?)[ \t]*((?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|<>()])+)")

_Word = tuple[str, str]  # (raw text, unquoted value)


def _can_fire(command: str) -> bool:
    """False when *command* cannot hold a recursive-force ``rm`` at all."""
    return bool(_CAN_FIRE_RE.search(command))


def _unquote(raw: str) -> str:
    return "".join(_shell._shell_tokens(raw)) if raw else ""


def _is_self_path(rest: str) -> bool:
    """True when *rest*, after a root or home prefix, still names that dir itself."""
    if rest.endswith("/*"):
        rest = rest[:-2]
    return rest == "" or (rest.startswith("/") and posixpath.normpath("/h" + rest) == "/h")


def _target_kind(word: _Word, home: str) -> "str | None":
    """``"root"`` / ``"home"`` when *word* is that directory itself, else None."""
    raw, value = word
    if value.startswith("/") and _is_self_path("/" + value.lstrip("/") if value.strip("/") else ""):
        return "root"
    match = _HOME_RAW_RE.match(raw)
    if match and _is_self_path(_unquote(raw[match.end() :])):
        return "home"
    if home and os.path.normpath(value.removesuffix("/*")) == home:
        return "home"
    return None


def _wipe_kinds(args: "list[_Word]", home: str) -> "set[str]":
    """Targets an ``rm`` with *args* wipes recursively and forcibly, else empty."""
    recursive = force = options_done = False
    operands: list[_Word] = []
    for word in args:
        arg = word[1]
        if options_done or arg == "-" or not arg.startswith("-"):
            operands.append(word)
        elif arg == "--":
            options_done = True
        elif arg.startswith("--"):
            name = arg.split("=", 1)[0]
            recursive = recursive or "--recursive".startswith(name)
            force = force or "--force".startswith(name)
        else:
            recursive = recursive or "r" in arg or "R" in arg
            force = force or "f" in arg
    if not (recursive and force):
        return set()
    return {kind for word in operands if (kind := _target_kind(word, home))}


def _heredoc_end(text: str, start: int, tags: "list[tuple[str, bool]]") -> int:
    """Offset just past the heredoc bodies that begin at *start*."""
    pos = start
    for tag, strip_tabs in tags:
        while pos < len(text):
            end = text.find("\n", pos)
            end = len(text) if end == -1 else end
            line = text[pos:end]
            pos = end + 1
            if (line.lstrip("\t") if strip_tabs else line) == tag:
                break
    return min(pos, len(text))


def _raw_commands(command: str) -> "list[list[str]]":
    """*command* cut into simple commands of RAW words, before quotes come off.

    Cuts at every operator, paren, backtick and newline the shared quote state
    machine (``_iter_shell_chars``) marks active, and splits words at active
    whitespace, so a quoted ``'a;b'`` or ``'#x'`` stays one word. ``2>&1`` and
    ``&>`` are redirections. A comment and each heredoc body are skipped, and the
    walk restarts unquoted after them, so a quote in either opens nothing. A live
    ``$(…)`` / ``<(…)`` / ``>(…)`` (bare or in double quotes) is walked as commands
    of its own, from an unquoted start as bash does, up to its own closing ``)``,
    and stands as one placeholder word in the command around it, so its quotes
    and heredocs never flip the outer walk.
    """
    commands, _end = _walk(_shell._fold_line_continuations(command), 0, False, 0)
    return commands


def _walk(text: str, start: int, nested: bool, math: int) -> "tuple[list[list[str]], int]":
    """The commands from *start*, and the offset where a *nested* body closed."""
    commands: list[list[str]] = [[]]
    word: list[str] = []
    tags: list[tuple[str, bool]] = []
    pos, state, ansi, depth = start, 0, False, 0

    def end_word() -> None:
        if word:
            commands[-1].append("".join(word))
            word.clear()

    while pos < len(text):
        resume, resume_state, resume_ansi = len(text), 0, False
        for step in _shell._iter_shell_chars(text[pos:], state, ansi):
            at = pos + step.offset
            opens_sub = text[at + 1 : at + 2] == "(" and (
                (step.text == "$" and step.state != 1) or (step.active and step.char in "<>")
            )
            if opens_sub:
                inner, end = _walk(text, at + 2, True, int(text.startswith("$((", at)))
                current = commands.pop()
                commands.extend(inner)
                commands.append(current)
                word.append("$_")
                resume, resume_state, resume_ansi = end, step.state, step.ansi
                break
            if step.active and step.char == "(":
                depth += 1
            elif step.active and step.char == ")":
                if nested and not depth:
                    end_word()
                    return commands, at + 1
                depth -= 1
            if step.active and text.startswith("((", at) and text[at - 1 : at] != "(":
                math += 1
            elif step.active and text.startswith("))", at) and text[at - 1 : at] != ")":
                math = max(0, math - 1)
            if step.active and step.char == "#" and not word:
                newline = text.find("\n", at)
                resume = len(text) if newline == -1 else newline
                break
            if step.active and step.char == "\n" and tags:
                end_word()
                commands.append([])
                resume = _heredoc_end(text, at + 1, tags)
                tags = []
                break
            if step.active and text.startswith("<<", at) and text[at - 1 : at] != "<":
                tag = _HEREDOC_TAG_RE.match(text, at)
                # Inside an unclosed active ``((`` a ``<<`` is a shift, not a heredoc.
                if tag and not math and not text.startswith("<<<", at):
                    tags.append((_unquote(tag.group(2)), bool(tag.group(1))))
            if step.active and step.char.isspace() and step.char != "\n":
                end_word()
            elif step.active and step.char in _CUT_CHARS and not _is_redirect_amp(text, at):
                end_word()
                commands.append([])
            else:
                word.append(step.text)
        pos, state, ansi = resume, resume_state, resume_ansi
    end_word()
    return commands, len(text)


def _is_redirect_amp(text: str, at: int) -> bool:
    """True for the ``&`` of ``>&`` / ``<&`` / ``&>``, which redirects, not cuts."""
    return text[at] == "&" and (text[at - 1 : at] in ("<", ">") or text[at + 1 : at + 2] == ">")


def _rm_wipe_targets(command: str) -> "set[str]":
    """The wipe targets (``root`` / ``home``) any ``rm`` in *command* hits."""
    if not _can_fire(command):
        return set()
    home = os.path.normpath(os.path.expanduser("~"))
    home = "" if home in ("~", "/") else home
    kinds: set[str] = set()
    for raw_words in _raw_commands(command):
        argv = [(raw, _unquote(raw)) for raw in raw_words]
        start = 0
        while start < len(argv) and (
            argv[start][1] in _KEYWORDS
            or ENV_ASSIGNMENT_RE.match(argv[start][1])
            or _LEADING_REDIRECT_RE.match(argv[start][1])
        ):
            start += 1
        if start < len(argv) and _shell._program_basename(argv[start][1]) == "rm":
            kinds |= _wipe_kinds(argv[start + 1 :], home)
    return kinds
