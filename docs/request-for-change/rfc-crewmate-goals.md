---
title: Crewmate goals — a long-term goal, short-term tracking, and a daily reminder
status: accepted
author: buluoray
created: 2026-10-12
last-audited: 2026-10-12
audited-at: d866dd2607
doc-pr:
implementation-prs: []
tracking-issues: [19240]
supersedes: []
superseded-by: []
---

# RFC: Crewmate goals

- Status: accepted. This is the product owner's decision, given 2026-10-12 by
  rayrayxu. Claims about today's code were checked at `d866dd2607` (main).
- Author: buluoray
- Related: [#19240](https://github.com/kirodotdev/KiroCrew/issues/19240) (the
  crewmate's own MCP server, `set_goal`), [#19258](https://github.com/kirodotdev/KiroCrew/pull/19258)
  (`update_self`), [rfc-crewmates-launch.md](rfc-crewmates-launch.md) § 02.

## 1. Summary

A crewmate gets a **goal** that is separate from its **About**:

- **About** says who the crewmate is. The crewmate manages it itself.
- **Goal** says what it is working toward: one **long-term goal**, and the
  **short-term goals** it is doing now to get there, each tracked as open or done,
  and up to three **key results** that say how far it is from the goal. The user changes both directly. The crewmate manages its
  short-term goals itself, with no sign-off, and proposes a new long-term goal
  through a change card the user confirms.
- Three switches on the Goals tab: a **Daily Goal Reminder** that keeps the
  crewmate on its goal, a **Weekly report** and a **Monthly review** for the
  user. Reports are shown on the crewmate's Dashboard side panel and saved as
  artifacts.

The Goals tab on the profile, which today is a "coming soon" card, shows the
long-term goal and its short-term goals.

## 2. Problem

Today one field does two jobs, and a third place that sounds like a goal stores
nothing.

| Where | What it does today |
|---|---|
| New crewmate card, Advanced: "What it looks after" | writes the crew config's `description` |
| A `crewmate.create` change card (a crewmate made from chat) | posts its `goal` as the crew config's `description`, and its optional `schedule` sends that goal as the job's message |
| Profile, About | shows the same `description` |
| Profile, Goals tab | a placeholder card, `profile_goals_soon` |
| The crewmate's first message | asks the user for its goal; the answer stays in the chat and is written nowhere |

So a person tells a new crewmate its goal, and the product loses it. Nothing on
the profile shows it, and nothing brings it back to the crewmate later. A goal
also has a shape a description does not: it changes over time, and it has parts
that get done.

## 3. Proposal

### 3.1 Two fields, two owners

| Field | Holds | Who changes it | How |
|---|---|---|---|
| About (`description`) | who the crewmate is, in a sentence or two | the crewmate; the user from the crew editor | `update_self` from #19258 |
| Long-term goal | what the crewmate works toward | the user directly; the crewmate by proposal | the Goals tab, or `set_goal` (a change card) |
| Key results | how the crewmate will know the goal is reached | the crewmate proposes them, the user confirms; the crewmate updates their progress directly | `set_key_results` (a change card), `key_result_progress` |
| Short-term goals | the steps toward it | the user and the crewmate, directly | the Goals tab, or the crewmate's short-term goal tools |

The card's Advanced field writes the **goal**, not the description, and is labelled
**Goal**. A create with no goal leaves it empty, and the crewmate's first message
asks for it as it does today. When the user answers, the crewmate proposes it with
`set_goal`, and the card saves it once the user confirms. A goal the user typed on
the card is saved as given.

A `crewmate.create` change card (`change_card_catalog.py`, the crewmate made from
chat) is the same: its `goal` writes the long-term goal into the goal record, not
`description`. Its optional `schedule` stays what it is today, the crewmate's own
work schedule, created as before.

### 3.2 What a goal holds

```text
goal
├─ long_term      one paragraph: what the crewmate is working toward
├─ key_results[]  how it will know it got there, at most 3
│  ├─ text      e.g. "PRs in a release have a description"
│  ├─ target    e.g. 27, or a word target such as "yes"
│  ├─ current   the crewmate's latest reading
│  ├─ updated_at
│  └─ readings[] every reading with its time, kept 90 days, for reports
└─ short_term[]   the steps it is taking toward it now, at most 20; at the cap a
                  new one evicts the oldest done or dropped item, and is refused
                  only when all 20 are open
   ├─ text
   ├─ status   open | done | dropped
   ├─ updated_at
   └─ updated_by   user | crewmate
```

It is stored as `trust/member-goals/<slug>.json` under the data home, a
gateway-only directory like the one the rules file lives in, and keyed like the
other member files (name-scoped, so a colliding slug never inherits another
member's goal). It is not beside `briefing.md`: the member's directory is
writable by the crewmate's own file tools, which would let it change its
long-term goal or key results without the user's button. Every write goes through
the gateway's routes and tools.

The record has the same protection as `member-rules/`: the tool gate keeps the
crewmate's file tools out of `trust/`, but the OS sandbox leaves `trust/`
writable, so a shell the crewmate spawns could still rewrite it. Sealing `trust/`
in the sandbox is an operator decision outside this RFC. Both the long-term goal and the short-term goals live in
this record, and neither is written to the protected `member-rules/` file: rules
are the user's safety boundary and stay out of the crewmate's reach, even through
a card. This replaces #19240's plan to write the goal into the rules file when a
`set_goal` card is applied; `set_goal` writes this record instead.

### 3.3 How the crewmate changes its goals

On the `kirocrew-mate` server from #19258:

| Tool | Does | Sign-off |
|---|---|---|
| `goal_read` | returns the long-term and short-term goals | none |
| `set_goal(long_term)` | proposes a new long-term goal | a change card the user confirms (#19240) |
| `set_key_results(items)` | proposes the key results, replacing the current set | a change card the user confirms |
| `key_result_progress(id, current)` | records a new reading for one key result | none |
| `short_goal_add(text)` | adds an open short-term goal | none |
| `short_goal_update(id, status?, text?)` | marks one done or dropped, or rewords it | none |

The long-term goal is what the user asked the crewmate to work toward, so the
crewmate cannot change it without the user's button. Short-term goals are the
crewmate's own working plan toward it. Adding one and checking one off should
feel invisible: the crewmate does it when it is confident, and the user simply
sees the Goals tab move. Every short-term change records who made it and when, and
the user can undo it, edit it or uncheck it on the Goals tab. The user edits both
levels on the Goals tab directly.

### 3.3a Key results come from the crewmate

The user says what the crewmate should work toward; the crewmate works out how to
tell when it got there. Once a long-term goal is set, the crewmate looks into the
work (the repo, the queue, the past releases it can read), then proposes one to
three key results with `set_key_results`, each something it can check itself and
report as a number or a plain yes/no. The card shows them; the user confirms,
edits or declines. A goal that cannot be measured gets no key results, and the
crewmate says so instead of inventing a number.

After that the crewmate keeps the readings current with `key_result_progress`
whenever it checks, with no sign-off. When the long-term goal changes, the
crewmate proposes a new set. The user can edit or remove a key result on the
Goals tab at any time.

### 3.4 Short-term goals and work-ledger tasks

This section amends [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md)'s
vocabulary: the "goal in flight" and "goals it left open" that the greeting reads
are work-ledger **tasks**. The greeting's behaviour is unchanged.

They stay separate. A work-ledger entry (what the Crew board and the greeting
card in [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) read) is one concrete
task the crewmate is running: fix a bug, watch a PR. It ends when the task ends. A
short-term goal is a step in the crewmate's standing plan and can span many tasks.

- The greeting keeps reading the work ledger, and its user-facing copy says
  **task**, not goal, so the two never share a word. Today the only greeting
  string that says goal is `pages.membersPage.resume_failed` ("Could not read
  where {{name}}'s goal stands."); it becomes "…where {{name}}'s task stands."
- When a task finishes and it completed a short-term goal, the crewmate checks
  that short-term goal off itself (§ 3.3).

### 3.5 Check-ins and reports

Three cadences, each with one job, so the user is not told the same thing three
times. Each has its own switch on the Goals tab, off by default, and none can be
turned on while the crewmate has no long-term goal. Each switch writes one
gateway-owned schedule for that crewmate and turning it off removes it. One time
of day (default 09:00 in the user's timezone) serves all three; the weekly report
picks a weekday and the monthly review a day of the month.

| Cadence | For | What happens | Where the user sees it |
|---|---|---|---|
| **Daily Goal Reminder** | the crewmate | a hidden turn: take a fresh reading of each key result, check off, drop or add short-term goals, ask only if blocked | one line in the thread, and the Dashboard tab updates |
| **Weekly report** | the user | how each key result moved this week (from its readings), short-term goals done and added, what is stuck, the plan for next week | a report artifact, linked from one line in the thread and a notification |
| **Monthly review** | the user | whether the long-term goal and key results still fit the work; if not, the crewmate proposes new key results or a new goal through a change card, raised from the review turn itself | a report artifact like the weekly one, with any card in the thread |

**Check-ins are not ordinary schedules.** The three schedules live in the
gateway's cron store, so they keep its persistence, timezone handling and
missed-run behaviour, but they are internal: they are not listed on the
dashboard's Schedule page or on the crewmate's Schedules tab, and they are not
counted there. The Goals tab switches are their only control.

**How this relates to the schedules a crewmate already has.** The Daily Goal
Reminder is the only goal check-in. A `crewmate.create` card's `schedule` is the
crewmate's work schedule (when it runs its job), not a reminder, and an existing
one is left untouched; the check-ins never read, replace or remove it. A monitor
loop (`monitor_start`, auto-nudge) belongs to one session and one task and ends
with it, so it does not overlap a standing goal. A crewmate that already has a
work schedule and turns the Daily Goal Reminder on gets both: the job, and the
check-in on how the job is going.

**Which turns may raise a card.** A card normally needs a turn the user sent.
The Monthly review is the one exception: the turn the gateway itself starts as a
member's Monthly review may propose that member's `set_goal` and
`set_key_results` cards, because asking whether the direction still fits is what
the review is for. The gateway recognises that turn from its own record of
starting it, never from anything the crewmate or a request says. The Daily
Goal Reminder and Weekly report turns, and every other scheduled, loop, app or
subagent turn, still cannot raise a card. As always, nothing changes until the
user presses it.

This amends [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md)'s
rule that a turn the user did not send gets `not_user_turn`, for these two card
kinds on this one turn only. It is acceptable because a card is a proposal, not a
change: the review may have read untrusted content, but whatever it proposes is
shown to the user as a before-and-after card and does nothing until they press
Apply, the card can only name the reviewing crewmate's own goal and key results,
and every other kind stays refused.

The weekly report says what moved; the monthly review asks whether the direction
is still right. Without the monthly step, key results set once would never be
revisited.

### 3.5a Where reports are shown

- **Dashboard side panel.** The crewmate's Dashboard tab
  ([rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md)) shows
  the live picture: the long-term goal, each key result's reading against its
  target with its trend, and the open short-term goals. These values are read by
  the host from the goal record, so the page cannot show a number the record
  does not hold.
- **Report artifacts.** Each weekly report and monthly review is saved as its own
  artifact, filed under `Crewmates/<name>/Reports`, so past reports stay
  browsable and shareable. The thread and the notification link to it; the chat
  does not carry the report's full text. The Dashboard tab lists the latest
  reports below the live picture.

### 3.6 The goal in the crewmate's context

The long-term goal and the open short-term goals are injected at session start, after the rules and before
the briefing, so the crewmate always knows what it is working toward without
calling `goal_read`.

### 3.7 The Goals tab

- The long-term goal at the top, editable in place.
- Under it, the key results, each with its reading against the target as a
  progress bar, and when it was last read. Empty until the crewmate's proposal is
  confirmed.
- Under it, the open short-term goals, each with a checkbox, who last changed it
  and when. Done and dropped ones hide automatically behind a **Show N done**
  disclosure, collapsed by default, so the list stays the work in front of the
  crewmate.
- An add box for a new short-term goal.
- The **Daily Goal Reminder**, **Weekly report** and **Monthly review** switches,
  the shared time, the weekday and the day of the month.

## 4. UI mock

Static mocks, not screenshots of shipped UI. The images are attached to this RFC's PR.

**Goals tab.** The long-term goal, the key results the crewmate proposed, the
short-term goals toward it, and the three check-in and report switches.

![Goals tab, desktop](https://github.com/user-attachments/assets/806f6412-9d53-4dfd-86f3-05e1abee5701)
![Goals tab, phone](https://github.com/user-attachments/assets/d029b3da-c553-4a70-92b2-26f0d020013c)

**No goal yet.** The check-in and report switches are off and cannot be turned on.

![Goals tab with no goal](https://github.com/user-attachments/assets/46befb44-2587-4b0e-9303-f314dfe5c972)

**The crewmate proposes its key results.** After researching the work, as one
change card the user confirms.

![Key results proposal, desktop](https://github.com/user-attachments/assets/0c274fd7-d1e2-4692-9030-353a9f0e19de)
![Key results proposal, phone](https://github.com/user-attachments/assets/383598db-af29-4d87-9763-b7fec9691915)

**A new long-term goal.** Only this needs sign-off: `set_goal` shows a change
card, and nothing changes until the user presses Apply.

![set_goal change card, desktop](https://github.com/user-attachments/assets/376f53ef-3679-495a-830f-1e95a90d340a)

**The daily reminder.** A hidden turn, so the thread shows only the crewmate's
update. Short-term goals it checked off or added appear as a note with Undo.

![Daily reminder, desktop](https://github.com/user-attachments/assets/303487f9-2389-4667-93ca-e1fc18d7f317)
![Daily reminder, phone](https://github.com/user-attachments/assets/98396bd9-1472-4b90-bba0-f55f04ca194b)

**Dashboard side panel.** The live goal picture read from the goal record, and
the latest reports.

![Dashboard side panel, desktop](https://github.com/user-attachments/assets/402adb27-540a-4a93-94df-0b2bd81e7dc9)
![Dashboard side panel, phone](https://github.com/user-attachments/assets/1af9d730-ed22-4d15-8bd4-df50238b62ae)

**Weekly report.** Saved as an artifact; the thread only links to it.

![Weekly report artifact](https://github.com/user-attachments/assets/9019ab69-b496-4e68-8330-47f268a25a9c)

**New crewmate card.** The Advanced field is **Goal** and writes the long-term
goal, not About.

![New crewmate card with the Goal field](https://github.com/user-attachments/assets/59697c71-d6e1-4341-9e44-9844b446883b)

## 5. Non-goals

- Goals shared across a team of crewmates.
- Deadlines, priorities or progress percentages on short-term goals.
- Reporting goals anywhere outside the crewmate's own thread and profile.
- Changing what the rules file holds or who can write it.

## 6. Decided: where the goal is stored

The product owner decided on 2026-10-12 that the long-term goal goes into the new
per-member goal record with the short-term goals (§ 3.2), not into
`member-rules/<slug>.md`. The rules file keeps holding rules only. The record
lives in `trust/member-goals/`, out of the crewmate's file tools' reach.

## 7. Existing crewmates

Crewmates created before this change are left as they are. Their `description`
is not moved, and they start with no goal and the reminder off.
