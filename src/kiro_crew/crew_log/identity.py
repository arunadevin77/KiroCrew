"""The one comparison of two SESSION IDENTITIES, and why it answers three values.

A slot key is a NAME. A closed session's name can be taken by a new one, so every
record about a session carries the identity of the incarnation it was written for, and
every reader of such a record has to ask whether that identity is the one asking.

That question has THREE answers, and a comparison written ``==`` or ``!=`` states only
two. The one it drops is "nobody can say", and dropping it is how a record that decides
nothing gets acted on as if it decided something. This module exists so the comparison
is made in one place, with all three answers named, and so a test can enumerate every
site that compares an identity and assert each one came through here.

Deliberately its own module with NO ``kiro_crew`` imports. The two readers that need it
sit on opposite sides of a layer boundary -- the crew log's durable manager read and the
dashboard's live manager check -- and the dashboard must not pull the log's storage
package onto its boot path just to compare two strings.
"""

from __future__ import annotations

from typing import Final

#: The record names the session asking. Its statement is about that session.
#:
#: Compared by NAME wherever a verdict is read. All three values are non-empty strings,
#: so ``if compare_identity(...)`` is always true -- a contract test pins that every
#: call's result meets one of these constants.
IDENTITY_SAME: Final = "same"

#: The record POSITIVELY names a different incarnation of the same name. That is a
#: statement, and a reader may act on it -- a walk looking for the asking session's own
#: record may pass this entry over and keep looking.
IDENTITY_OTHER: Final = "other"

#: NOBODY CAN SAY which incarnation the record is about: one side carries no identity,
#: or the field that held it cannot be found. Not a statement about anything, so a
#: reader owes it an undecided answer of its own rather than its "not mine" branch.
IDENTITY_UNKNOWN: Final = "unknown"


def compare_identity(recorded: str, asking: str) -> str:
    """Whether *recorded* and *asking* name the same session: SAME, OTHER or UNKNOWN.

    THE ONE PREDICATE that compares two session identities in the manager feature, and
    it answers three values because two of them are not the same fact:

    * the record POSITIVELY names another incarnation -- a decided "not about you",
      which a walk may pass over and keep looking;
    * nobody can say WHICH incarnation the record is about -- a field that is missing,
      empty, or whose key was damaged so the read finds nothing -- which states nothing
      and may not be acted on like a statement.

    Collapsing the second into the first is fail-OPEN wherever a walk passes an entry
    over, and the manager read is such a walk: it takes the NEWEST entry about the
    asking session, so an entry nobody can attribute makes the newest statement
    invisible. The walk falls off the end, "no handoff was recorded" comes back as a
    DECIDED fact, and the creator that a handoff revoked has its reach again. One
    damaged byte in a field NAME reaches that, with nothing about the grant itself
    unreadable and no attacker involved.

    A caller MAY end OTHER and UNKNOWN in the same place -- several do, because "this
    notice has no recipient" is the right answer to both. What it may not do is reach
    that place without the two having been told apart first: the comparison is what
    computes three answers, and the caller then decides each one. That is the difference
    between a site that is safe and a site that is safe by accident, and only the first
    kind survives somebody giving it a second use.
    """
    if not recorded or not asking:
        return IDENTITY_UNKNOWN
    return IDENTITY_SAME if recorded == asking else IDENTITY_OTHER
