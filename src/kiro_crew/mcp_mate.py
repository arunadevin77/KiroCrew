"""MCP server ``kirocrew-mate`` — a crewmate changes its own record.

The tools are the crewmate's own settings, each named for what it does, in two
tiers that follow who owns the field:

* **Direct** -- ``update_self``: the presentation the crewmate owns (its display
  name, its description, its ghost face). The gateway writes the record at once
  and draws one row in the crewmate's chat with an **Undo** the user can press;
  nothing waits on a card. Only when the user has just asked for the change:
  the tool description says so, and the gateway admits only a turn the user
  sent (:mod:`kiro_crew.dashboard.handlers.member_self`).
* **Card-backed** -- ``set_model``, ``set_approval_mode``,
  ``request_capabilities``: what the crewmate runs on and what it may do
  without asking. These are the user's to grant, so each becomes the
  existing change card (:mod:`kiro_crew.dashboard.change_cards`) for the caller's
  own member, and nothing changes until the user presses it. The crewmate names
  the ask; the gateway fixes the kind and the member. The outcome reaches the
  crewmate on its next turn, the way every card outcome does.

Why its own server
------------------
The server is the unit of assignment, authorization and governance: an operator
or a policy withholds the whole set at once, and the set's grants
(``agent._MATE_AUTO_GRANTS``) are reviewed together. It is mounted on every
crewmate; the guide server (``kirocrew-guide``) stays what it is, the user's
help with the dashboard. Every tool here acts on the CALLING crewmate -- never
on another, never on a name passed in -- and refuses off the dashboard (a
channel, a schedule, a subagent, the CLI) with a one-line reason.

Stateless: every call is one round trip to the gateway, which owns the record,
the undo and the cards. Nothing here holds per-caller data between calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.mcp_core import (
    _post,
    _resolve_session_key,
    require_strict_session_key,
)
from kiro_crew.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import MAX_SHORT_STRING, MCP_MATE_SCHEMAS, validate_tool_args

logger = logging.getLogger(__name__)

SERVER_NAME = "kirocrew-mate"
SERVER_VERSION = "1.0.0"

#: The trait axes of the ghost face and the values the shipped renderer draws.
#: Told to the model in the tool description so it names a real option; an
#: unknown value is kept by the validator and renders as the axis's default.
GHOST_TRAIT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "eyes": (
        "canon",
        "closed",
        "sleepy",
        "wink",
        "wide",
        "sparkle",
        "visor",
        "glasses",
        "cross",
        "squint",
        "swirl",
        "heart",
        "cyclops",
    ),
    "brows": ("none", "raised", "angry", "flat"),
    "mouth": ("none", "smile", "open", "cat", "oh", "grin", "tongue", "wobble", "smirk"),
    "accessory": (
        "none",
        "antenna",
        "halo",
        "cap",
        "phones",
        "bow",
        "crown",
        "beanie",
        "party",
        "flower",
        "bandana",
        "hardhat",
    ),
    "prop": ("none", "mug", "glass", "wrench", "bolt", "star", "term", "heart"),
}

_ONLY_WHEN_ASKED = (
    "Call it only when the user has just asked for exactly this change, in their "
    "own words; never on your own initiative, never to change anyone else. Only a "
    "crewmate in its own chat can call it, from a message the user sent in the "
    "dashboard."
)

_CARD_NOTE = (
    "Nothing changes when you call this: the user sees a card with a button in "
    "this chat and decides. Write ONE reply that refers to the card and end your "
    "turn; the outcome is given to you at the start of your next turn."
)


def _tool_definitions() -> list[dict[str, Any]]:
    vocab = "; ".join(f"{axis}: {', '.join(vals)}" for axis, vals in GHOST_TRAIT_VOCABULARY.items())
    return [
        {
            "name": "update_self",
            "description": (
                "Change what the user sees of you: your display name, your description "
                "(a short line about what you do), or your ghost face. Applied at once, "
                "shown in this chat as one line the user can undo. Pass only the fields "
                "the user asked to change, with the values exactly as they gave them. "
                'avatar is {"traits": {...}} naming one or more axes (' + vocab + "; "
                "blush and flip are true/false; tile is a #rrggbb background colour). An "
                "axis you leave out keeps its current value, or the plain Kiro mark when "
                "your face was never pinned. A crewmate wearing an uploaded picture or an "
                "appearance pack keeps it: that face is changed in the crew editor. "
                + _ONLY_WHEN_ASKED
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "display_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_SHORT_STRING,
                    },
                    "description": {"type": "string", "maxLength": 4000},
                    "avatar": {
                        "type": "object",
                        "properties": {
                            "traits": {
                                "type": "object",
                                "properties": {
                                    "eyes": {"type": "string"},
                                    "brows": {"type": "string"},
                                    "mouth": {"type": "string"},
                                    "accessory": {"type": "string"},
                                    "prop": {"type": "string"},
                                    "blush": {"type": "boolean"},
                                    "flip": {"type": "boolean"},
                                    "tile": {"type": "string", "maxLength": 7},
                                },
                                "additionalProperties": False,
                            }
                        },
                        "required": ["traits"],
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "set_model",
            "description": (
                "Ask the user to change the model or the reasoning effort your sessions "
                "run on. Pass an id from GET /api/models (the dashboard's model list) "
                'or "" to inherit the default. ' + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "maxLength": 64},
                    "reasoning_effort": {"type": "string", "maxLength": 16},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "set_approval_mode",
            "description": (
                "Ask the user to change how your own chat approves tools: normal (ask "
                "before every action), trust_reads (act on reads, ask before changes) "
                "or trust (act without asking). " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "approval_mode": {"type": "string", "enum": ["normal", "trust_reads", "trust"]},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["approval_mode"],
                "additionalProperties": False,
            },
        },
        {
            "name": "request_capabilities",
            "description": (
                "Ask the user for a change to your tools, tool approvals, MCP servers "
                "or skills. draft is {operations: [{section, id, action, value?}], "
                "enroll?} in the shape get_member_capabilities (kirocrew-guide) "
                "returns for you; call that first. " + _CARD_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "draft": {"type": "object"},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["draft"],
                "additionalProperties": False,
            },
        },
    ]


def _list_tools() -> list[dict[str, Any]]:
    """Unconditional: reaching this process means a spec granted the set."""
    return _tool_definitions()


def _strict_session_key() -> tuple[str, str]:
    return require_strict_session_key(
        "this session's identity could not be verified strictly, so it is not a "
        "crewmate's own chat (a subagent has none; ask from the parent session)",
        SERVER_NAME,
    )


#: Gateway refusal codes that mean "this turn is not a crewmate's own dashboard
#: turn" (``handlers.guide._resolve_agent_caller`` and ``handlers.member_self``).
_OFF_DASHBOARD_CODES = frozenset(
    {
        "no_live_slot",
        "no_dashboard_turn",
        "channel_caller",
        "subagent_caller",
        "unattended_caller",
        "app_caller",
        "app_scoped_caller",
        "missing_session_key",
        "not_user_turn",
        "unattested_caller",
        "not_a_crewmate",
    }
)


def _off_dashboard(name: str, reason: str) -> str:
    reason = reason.removeprefix("Error: ").strip().rstrip(".")
    return redact(
        f"Error: {name} needs your own chat in the dashboard: {reason}. Nothing changed; "
        "do not say it did."
    )


def _error(d: dict[str, Any]) -> str | None:
    if d.get("error"):
        code = d.get("code")
        return redact(f"Error: {d['error']}" + (f" ({code})" if code else ""))
    return None


def _error_for(name: str, d: dict[str, Any]) -> str | None:
    if d.get("error") and d.get("code") in _OFF_DASHBOARD_CODES:
        return _off_dashboard(name, str(d["error"]))
    return _error(d)


def _render(result: dict[str, Any]) -> str:
    return redact(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = MCP_MATE_SCHEMAS.get(name)
    if schema is None:
        return args
    return validate_tool_args(args, schema)


_TOOL_NAMES = frozenset(MCP_MATE_SCHEMAS)

#: Tool -> the gateway's request name for a card-backed ask.
_PROPOSALS = {
    "set_model": "model",
    "set_approval_mode": "approval_mode",
    "request_capabilities": "capabilities",
}


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name not in _TOOL_NAMES:
        return f"Error: unknown tool '{name}'"
    sk, err = _strict_session_key()
    if err:
        return _off_dashboard(name, err)

    if name == "update_self":
        fields = {k: args[k] for k in ("display_name", "description", "avatar") if k in args}
        if not fields:
            return "Error: update_self needs at least one of display_name, description, avatar."
        d = _post("/api/member/self/update", {"fields": fields}, session_key=sk)
        refused = _error_for(name, d)
        if refused:
            return f"{refused}\nNothing about you changed; do not say it did."
        changed = d.get("changed") or []
        if not changed:
            return _render({**d, "next": "Those were already your values; nothing changed."})
        shown = {
            "display_name": "Your name now shows as "
            + str((d.get("after") or {}).get("display_name") or "")
            + " everywhere in the dashboard; answer to it from now on.",
            "description": "Your description is updated on the roster and in your prompt.",
            "avatar": "Your face is updated everywhere the dashboard shows you.",
        }
        return _render(
            {
                **d,
                "next": " ".join(shown[k] for k in changed if k in shown)
                + " The user sees one line about it in this chat with an Undo; say what "
                "changed in one sentence and do not offer to undo it yourself.",
            }
        )

    params = {k: v for k, v in args.items() if k != "reason"}
    body: dict[str, Any] = {"request": _PROPOSALS[name], "params": params}
    if args.get("reason") is not None:
        body["reason"] = args["reason"]
    d = _post("/api/member/self/propose", body, session_key=sk)
    refused = _error_for(name, d)
    if refused:
        return f"{refused}\nNo card was shown; answer in words."
    return _render(
        {
            **d,
            "next": "The card is in this chat above your reply. Refer to its button "
            "once and end your turn; nothing changes until the user presses it.",
        }
    )


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    """Guarded entry point — schema validation and SEL audit live in the wrapper."""
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key=_resolve_session_key() or SERVER_NAME,
        downstream_service=SERVER_NAME,
    )


#: Consumes the per-call caller block the gateway injects rather than reading
#: identity from its own process, and refuses a caller the gateway cannot name.
#: Kept in step with ``mcp_discovery._MANAGED_SERVERS_CALLER_AWARE``.
ADVERTISE_CALLER_IDENTITY = True


def run_mcp_server() -> None:
    """Run the MCP stdio server — reads JSON-RPC from stdin, writes to stdout."""
    run_mcp_stdio_loop(
        SERVER_NAME,
        SERVER_VERSION,
        _list_tools,
        _call_tool,
        advertise_caller_identity=ADVERTISE_CALLER_IDENTITY,
    )


if __name__ == "__main__":  # pragma: no cover - process entry
    logging.basicConfig(level=logging.INFO)
    run_mcp_server()
