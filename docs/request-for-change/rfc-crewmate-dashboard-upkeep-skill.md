---
title: Crewmate dashboard upkeep skill -- a generated per-board skill for the fields only the crewmate can fill, named by one pointer line that survives compaction
status: in-progress
author: Raymond Chen, with kirocrew-worker
created: 2026-10-11
last-audited: 2026-10-11
audited-at: a84f616557
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate dashboard upkeep skill

- Status: in-progress. The design is the product owner's ruling of 2026-10-11:
  fold-backed fields already update themselves, so nothing new is built for
  them; a generated per-board skill covers only the fields the crewmate writes;
  a generated helper script fetches the data that lives outside the crew log;
  one pointer line names the skill. This document lands first because it adds a
  line to every crewmate's prompt and a section to the profile, and the First
  Principles lane reads that decision off the base.
- Related: [rfc-crewmate-dynamic-dashboard.md](rfc-crewmate-dynamic-dashboard.md)
  (the page, its manifest, the `[DASHBOARD]` block). Open PRs it meets:
  [#18707](https://github.com/kirodotdev/KiroCrew/pull/18707) (writes checked
  against the v3 package Model), [#18687](https://github.com/kirodotdev/KiroCrew/pull/18687)
  (layout lock), [#18957](https://github.com/kirodotdev/KiroCrew/pull/18957)
  (fold-backed conductor drawer).
- Measured at `a84f616557`.

## 1. Problem

A crewmate that adopts a Dashboard page is told WHICH fields are its own and
never what they mean, when to write them, or how to get the data. The whole
instruction is the `[DASHBOARD]` block (`dashboard_agentic.turn_block`):

```
[DASHBOARD]
Your dynamic dashboard is template project-report v1; the Dashboard tab shows it
only with the 'Dynamic Dashboard' Feature Preview on.
Fields you write: ci, for_you, verdict. Call dashboard_fields for their shapes and
current values before dashboard_write.
```

So fields go unwritten, or stay stale until the person chases them. A field such
as `ci` is worse: its data is on GitHub, and the crewmate has to work out the
`gh` calls and the payload shape every time.

`[CURRENT ASSIGNMENT]` (`members/<slug>/briefing.md`) cannot carry this. It is
agent-written, may be empty, and a crewmate with no file tools cannot write it.
The person also cannot see what the crewmate is told about its page.

## 2. Prior art: the fold half already updates itself

A field whose manifest source is `{"fold": "<name>", "path": "a.b"}` is filled
by the gateway from the crew log. Nobody writes it. The rule is the same in both
manifest formats:

- template manifest: `dashboard_templates/manifest.py` `_field`;
- v3 package Model: `artifact_store/dashboard_package.py` `_validate_source`,
  where "A field the agent writes itself declares `{'agentic': true}` instead".

So the upkeep gap is exactly the `{"agentic": true}` fields. This design builds
nothing for fold fields.

## 3. Goals and non-goals

Goals:

1. Every crewmate with a page has a generated upkeep skill covering its
   agentic fields only: what each means, when to write it, how.
2. An agentic field whose data lives outside the crew log gets a helper script
   beside the skill. The script fetches the data and prints a ready
   `dashboard_write` payload.
3. The skill is regenerated whenever the page in force or its field set changes.
4. One pointer line, injected wherever `[DASHBOARD]` is, names the skill. It
   survives compaction.
5. The person can read the skill on the profile.

Non-goals:

- Anything for fold fields (§2).
- The hand-published drawer (`panel_publish`). #18957 makes the conductor's
  drawer fold-backed.
- Running the helper for the crewmate. The crewmate runs it and calls
  `dashboard_write`; the write path and its checks are unchanged.
- Helpers for an agent-composed v3 package. A package carries no code (§4.3).

## 4. Design

### 4.1 The manifest declares upkeep per agentic field

An agentic field may declare:

```json
"ci": {
  "type": "object",
  "source": {"agentic": true},
  "upkeep": {
    "means": "the CI lanes of the PR this board watches, red ones by name",
    "when": "after every CI run you watch finishes or changes state",
    "how": "run the helper with the PR number and write what it prints",
    "helper": "project_report_ci.py"
  }
}
```

- `means`, `when`, `how`: non-empty strings, at most 200 characters each.
- `helper`: optional, a `.py` file name under the template's own `helpers/`;
  `load_template` refuses a manifest whose helper is not there.
- Refused on a fold field, and with an unknown key, like any manifest problem.
- Every built-in agentic field declares `upkeep`. The two whose data is on
  GitHub, `project-report.ci` and `pr-watch.lanes`, declare a helper.

### 4.2 The generated skill

`.dashboard-upkeep/<slug>/SKILL.md` under the skills dir, written by the
gateway. One section per agentic field:

```
---
name: .dashboard-upkeep/code-reviewer
description: Keep code-reviewer's Dashboard fields current (template project-report v1)
source: generated
---
# Dashboard upkeep: project-report v1

Fold fields fill themselves from the crew log. These are yours:

## ci
- Means: the CI lanes of the PR this board watches, red ones by name.
- When: after every CI run you watch finishes or changes state.
- How: run the helper with the repo, PR and task item id, set each lane owner,
  and write the value. Helper: `scripts/project_report_ci.py`, beside this file.
```

- A field with no `upkeep` gets a default section: its name, type and shape
  from `dashboard_fields`, "When: in the same turn what it shows changes".
- A page with no agentic field gets a skill of one line: the page updates
  itself; do not republish it.
- The text is read from the SHIPPED catalog entry, never from the crewmate's
  copy in `members/<slug>/dashboard/instance.json`. That copy is on the
  agent-writable side, and `instance.edit` may change it.
- The leading dot keeps it out of the skills index: the skill tree walk already
  skips dot directories. It is reached by the pointer line, so it costs no other
  session an index line, and no listing code changes.

### 4.3 The helper script

`.dashboard-upkeep/<slug>/scripts/<helper>` is a COPY of the shipped file
`dashboard_templates/builtin/<id>/helpers/<helper>`. It runs `gh`, reads the data,
and prints one JSON object, `{"field": "ci", "value": {...}}`, shaped to the
field's declared shape.

- Only a shipped built-in template may name a helper. A user template or a v3
  package is declarative and carries no code, the rule `manifest.py` already
  states, so neither gets a script.
- Copied, not generated: the bytes are reviewed in the PR that ships them.
  `skills_script_validator` scans LLM-generated candidates and is not on this
  path.
- A test runs each shipped helper against a recorded `gh` reply and checks its
  output passes `dashboard_agentic.check_write` for its field.

### 4.4 When it is regenerated

One function, `dashboard_upkeep.sync(slug)`, renders the skill and scripts and
writes only what differs from disk. It removes a script the page no longer names,
so a page swap cannot leave the old board's helper behind, and it puts back a
hand edit. Two callers:

| caller | why |
|---|---|
| `instance._commit` | adopt, apply, rollback and edit all commit through it |
| the `[DASHBOARD]` block build | a crewmate on the default page never commits; a gateway upgrade can change the shipped text |

A lock (#18687) changes no field, so it does not regenerate.

### 4.5 The pointer line

`turn_block` gains one last line:

```
Upkeep: before you write a field, read <skills dir>/.dashboard-upkeep/code-reviewer/SKILL.md
-- what each field means, when to write it, and how.
```

It rides wherever `[DASHBOARD]` rides: fresh session, post-compaction
re-injection (`context_assembly/turn.py` `post_compaction_parts`, member section
re-injected by the caller) and slim resume. The member section is not rebuilt
every turn, so `dashboard_apply` and `dashboard_rollback` also return the new
`[DASHBOARD]` block, pointer included, as `dashboard` in their reply.

### 4.6 Profile

`GET /api/members/{slug}/briefing` gains `upkeep`: `{"pointer", "skill",
"scripts"}` -- the pointer line, the skill text, and the helper file names, or
`null` when there is no page. Same owner gate and slug checks as the notes. The
Notes tab shows it first, read-only, under `Dashboard upkeep`.

## 5. Rejected designs

| design | why not |
|---|---|
| Guide text only: an `[OPERATING GUIDE]` block with one "when" line per field, injected beside `[DASHBOARD]` (this document's first draft) | Says when, never how. A field like `ci` still needs the crewmate to find the `gh` calls and the payload shape each time, and the full guide is paid for in every build. |
| One generic manifest-driven update script that fills every field | The fold half already updates itself (§2). The agentic half is judgment (`verdict`, `for_you`) or a per-source fetch (`ci`), and one script cannot do either. |
| A script per board | Most agentic fields have no external data. A helper exists only where one does. |
| Writing upkeep into `[CURRENT ASSIGNMENT]` | Agent-owned, may be empty, and not writable by a crewmate with no file tools. |

## 6. Tests (red before the change)

1. Apply page A, then page B: the skill names B's fields, not A's.
2. The post-compaction member section carries the pointer line.
3. A zero-agentic page gives the one-line skill.
4. Each shipped helper's output passes `check_write` for its field.
5. `upkeep` text in the crewmate's copy does not reach the skill.
6. The briefing read returns `upkeep`; the Notes tab renders it.

## 7. Rollout

1. This document.
2. One implementation PR: the manifest key, the built-in texts and two helpers,
   `dashboard_upkeep.sync`, the pointer line, the apply / rollback reply, the
   briefing key and the Notes section.
3. When #18707 lands, the v3 package Model gains the same `upkeep` key
   (without `helper`), and `sync` reads the Model in force.
