---
title: Work-ledger cache keeps every record — the fold is the bounded projection, the cache is the durable record, and the create check reads open items
status: draft
kind: decision
author: akushonkamen (drafted for maintainer decision)
created: 2026-10-12
last-audited: 2026-10-12
audited-at: c4c51aac5
doc-pr:
implementation-prs: []
tracking-issues: [19406]
supersedes: []
superseded-by: []
---

# RFC: Work-ledger cache keeps every record

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in §8 with the date. Nothing is on
> main. Verified at `c4c51aac5`: nothing trims the items directory — a closed
> item's record and its events file stay on the board for its whole life, bounded
> only by the 256 lifetime-create ceiling (`MAX_STORED_ITEMS_PER_CONDUCTOR`) that
> [#19068](https://github.com/kirodotdev/KiroCrew/pull/19068) removes — while the
> accepted [rfc-work-fold-forget-closed.md](rfc-work-fold-forget-closed.md) §4.2
> requires closed records past the recent-closed window to leave the cache. This
> document lands first because the rebuild rules #19068 ships read this decision
> off the base branch: they are written as if the cache legitimately holds what
> the fold forgot, and this document is what makes that legitimate.

## 1. Summary

The item cache keeps every record the writer minted, for the board's whole life.
The `work` fold stays the bounded projection — open items, the recent-closed
window, tombstones, counters. The invariant "the cache keeps the same set the
fold keeps" is retired and replaced: the fold keeps a bounded subset of the
cache, and the cache is the durable per-item record. In exchange, the store
takes on one new requirement that the retired invariant was implicitly paying
for: `_create_item`'s open-count backpressure check must read records
proportional to the OPEN items, never to the board's lifetime creates.

This amends [rfc-work-fold-forget-closed.md](rfc-work-fold-forget-closed.md)
§4.2 (the cache-parity paragraph, "Closed records past the recent window leave
the cache") and the `work_ledger.py` row of its §5 change table (the "trim
closed records past the window under the conductor lock" cell). That document is
accepted and is not edited here; this one records the amendment and its own
acceptance separately, per the convention that an amendment to an accepted RFC
lands in its own document.

Raised by issue [#19406](https://github.com/kirodotdev/KiroCrew/issues/19406),
from the design review on #19068 (CONCERNS, advisory).

## 2. Background

The forget-closed RFC asked the cache to follow the fold: a closed record past
the recent-closed window leaves `work-ledger/<slot>/items/`, trimmed under the
conductor lock, so the cache and the fold hold the same set. #19068 implements
the fold side and ships the cache side UNTRIMMED — the trim was taken out during
its review because it deleted the only whole copy of a closed item's worker
history. The fold forgets the item to a tombstone plus a counter; the crew log
still holds the raw entries, but nothing re-derives a coherent per-item record
from them on demand (fetching forgotten closed items back is a stated non-goal
of #19068), so after a trim the cache file would be the copy's only home, and
the trim deletes it.

What the shipped shape leaves open, and what #19406 pins down:

- `_create_item`'s open-count check reads `list_work_items`, which globs and
  parses every `it_*.json` record on the board. Every create pays
  O(lifetime records). On main the lifetime ceiling caps that at 256 parses;
  the moment #19068 removes the ceiling, the cost of a create starts climbing
  with the board's age, unbounded.
- The rebuild carries rules that only make sense while the cache holds records
  the fold has forgotten — a closed record the fold does not name is forgotten,
  not corrupt; an unrecorded legacy record counts toward the create total; a
  torn record on a board that forgot items keeps its files. These read today as
  transitional allowances for an un-trimmed cache. They are not transitional.
  Under either resolution of #19406 they are permanent, because option 2 (the
  trim) only reaches parity again after every forgotten record is destroyed.

## 3. The decision this asks for

Three parts. The first two retire a requirement; the third adds one.

**The cache keeps every record.** The items directory is the durable record of
the board: one JSON record and one events file per item the writer minted, for
the board's whole life. Nothing trims it as a matter of the fold's memory
management. The crew log remains the append-only raw history; the cache remains
the addressable per-item copy that the board, the probes and a rebuild read.

**The rebuild's forgotten-record rules are permanent.** A closed record the fold
does not name is a forgotten record, not corruption. The legacy-record backfill,
the torn-record carve-out on forgetting boards, and the strict reads on the
rebuild's pass over unnamed records stay exactly as #19068 ships them. The
amendment makes no change to the rebuild; it stops those rules from reading as
debt waiting for the trim to land.

**The create check reads open items, not lifetime records.** The open fan-out
cap (`MAX_ITEMS_PER_CONDUCTOR`, 32) is a property of the live set, and the
backpressure check that enforces it must cost a function of the live set. The
bound this amendment states: a create reads O(open items) records, whatever the
board's lifetime total. The mechanism named in #19406 is an open-item index, so
the check stops scanning closed records. One constraint any such index inherits
from the store's total lock order (conductor → item → binding): a close flips
the item's state under the ITEM lock, and no path may take the conductor lock
from inside an item lock — the rebuild holds the conductor lock and then every
item lock, so a close that waited on the conductor lock while holding an item
lock deadlocks against it. The index therefore cannot be "maintained under the
conductor lock" the way the retired trim cell promised; its writer discipline
(repair-on-read, or a lock of its own placed in the order) is design work that
belongs with the implementation, and the implementation belongs with the open
#19068 series or a follow-up stacked on it — not with this document.

The reader paths are not given a new bound by this amendment. Board listings and
the probes may keep the derived full scan, whose cost is a property of the
record count on disk; the dashboard board renders from the fold, and §7 asks
whether the readers should get a bound in the same change.

## 4. Case against

Keeping every record makes the items directory grow with the board's lifetime
creates, which is the shape the forget-closed RFC was written to end. The cost
is stated plainly: disk and per-record bytes climb for as long as the board
lives, and every derived scan (listing, probes) reads all of it.

The defence is that this is the system's accepted posture for history. The crew
log is append-only and unbounded by design; the cache is the per-item projection
of the same entries, so a cache that grows with lifetime creates grows with
history the store already keeps, not with something new. What this amendment
does NOT claim is that the growth is free — it claims the growth is the wrong
thing to delete (the record is the only whole copy of a closed item's history)
and the wrong thing to keep paying on the write path (hence the create-check
bound), and that the reader-path cost is a visible, measured property of a
directory a person can list, not a hidden one.

## 5. Alternatives considered

**Build the trim the RFC specifies (option 2).** Rejected for now. The trim's
review blocker has no placed home: the fold forgets a closed item to a tombstone
and a counter, the crew log holds raw entries no reader re-derives into an item,
and every candidate home for the record is machinery this repo has already
turned down — the generation archive (forget-closed §6, rejected on Windows
atomicity), a new archive store (new writer, new failure modes, no design), or
crew-log re-derivation (a stated non-goal of #19068). If a maintainer picks
option 2, this document is rejected and the placement design comes first; the
rebuild rules above would then genuinely be transitional, which is why §8 asks
the question rather than presuming the answer.

**Keep every record and keep the full scan.** Rejected. It is today's shape and
the defect #19406 raises: the create path reads lifetime records forever. The
bound is the point of the amendment; the cache decision alone would only
document the status quo.

**Split open from closed in the directory layout** (`items/open/`,
`items/closed/`), so the create check's glob skips closed records with no index.
Rejected: a close would move the record under the item lock, and moving a file
whose store is mid-write is the same non-atomic move that rejected the
generation archive on Windows; it also re-paths every glob the store, the probe
and the rebuild run today.

**Track the open count on the conductor header.** Rejected: creates hold the
conductor lock, but closes do not, and a close that took the conductor lock to
update the count inverts the documented total lock order and deadlocks against
the rebuild (§3's constraint).

## 6. Consequences

- #19068 lands unchanged. Its rebuild rules, its fold behaviour and its
  cache-keeps-every-record posture are what this document ratifies; only the
  §5 trim cell of the forget-closed RFC dies.
- A follow-up in the #19068 series adds the create-check bound. Until it lands,
  a create reads O(lifetime) records on an unbounded board — the defect #19406
  raises stays open until then, which is why the bound is part of the decision
  and not an optional extra.
- Forget-closed §7 question 2 (should the board UI fetch older closed items from
  the crew log on demand?) gains a cheaper third answer: read them from the
  cache, which now provably holds them. Not required by this amendment; noted
  so the UI work does not build the crew-log path by default.

## 7. Questions for maintainers

So maintainers are choosing between:

| option | closed-item history | per-create cost | rebuild rules |
|---|---|---|---|
| this document (cache keeps everything + create-check bound) | whole, in the cache | O(open items) once the bound lands; O(lifetime) until it does | permanent as shipped |
| build the trim (option 2 of #19406) | needs a placed home first, or the only whole copy is deleted | O(open items) after the trim | transitional until parity returns |
| keep everything, keep the scan | whole, in the cache | O(lifetime) forever | permanent as shipped |

1. Accept that the cache keeps every record — retiring the forget-closed §4.2
   parity paragraph and the §5 trim cell — or hold for the trim with a history
   home designed first?
2. Does the create-check bound land inside the open #19068 series, or as a
   follow-up stacked on it?
3. Should the reader paths (board listings, probes) take a bound in the same
   change, or stay derived scans whose cost tracks the directory a person can
   list?

## 8. Acceptance

Proposed 2026-10-12 by akushonkamen. Not yet accepted. A maintainer accepting
this document records it here with their name and the date, and the status above
moves to `accepted` in the same change. Drafted with AI assistance (Claude); the
author can explain every line of the document and the cited code.
