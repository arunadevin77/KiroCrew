"""Review-workflow tests for the First Principles lanes: the problems-first shape of
what they publish.
"""

from __future__ import annotations

from ai_review_workflows_helpers import (
    FP_LANES,
    _fp_contract,
    _reads_header_anchored,
    _review_contract_module,
    _workflow,
)


class TestFirstPrinciplesProblemsFirstContract:
    """The lane's PASS was praise by construction, and its calibration made
    BLOCK unreachable for the one class where the reviewer is structurally
    unable to check the premise. Both were prompt text, so both are pinned
    here: provenance for a claimed defect, symmetry as INHERITED, a deleted pin
    as a prior decision, an availability-path premise as the BLOCK case, and an
    output that leads with the problems rather than with the inventory."""

    def test_a_claimed_defect_needs_a_provenance_the_reviewer_can_point_at(self) -> None:
        # A `fix` whose only support is the description asserting a defect is
        # indistinguishable from an addition: a change shipped a whole-platform
        # regression behind "verified security finding" and no repro.
        contract = _fp_contract()
        assert "PROVENANCE OF A REPORTED DEFECT" in contract
        assert "a test\n     this PR adds that fails on base" in contract
        assert "linked issue" in contract
        assert "has no provenance you can check, so the item is INHERITED" in contract

    def test_symmetry_with_a_twin_is_never_a_justification(self) -> None:
        # The measured failure: the lane tagged "aligns win32 with the macOS
        # twin" as `justified`, which its own provenance lens already calls
        # INHERITED. The tag has to be spelled out or the general rule loses.
        contract = _fp_contract()
        assert "SYMMETRY IS INHERITED, ALWAYS" in contract
        assert "aligns X with its twin" in contract
        assert "are NEVER a\n     justification on their own" in contract
        assert "the symmetry form of\n     INHERITED" in contract
        # The twin is not evidence about this side.
        assert "nameable WITHOUT the twin" in contract

    def test_a_deleted_pin_is_a_prior_decision_not_a_gap(self) -> None:
        # The PR deleted a test whose message said the opposite and called the
        # pin "a gap". That is the framing-contradicted-by-the-diff BLOCK
        # trigger, so the contract must route it there by name.
        contract = _fp_contract()
        assert "A DELETED OR REWRITTEN\n   PIN IS A PRIOR DECISION" in contract
        assert "pinned the OPPOSITE behaviour" in contract
        assert "Treat it as standing" in contract
        assert 'calls\n   it "a gap"' in contract
        assert "framing contradicted by the diff" in contract
        assert 'a deleted pin recast as "a gap" with no' in contract
        # A diff that deletes a comment reading "This deliberately supersedes
        # the earlier ... pill spec" plus its pin tests, with a description that
        # says nothing about it, is the same BLOCK case as one that mislabels
        # the deletion: a trigger naming only the MISLABELLED form lets the
        # UNMENTIONED form fall to the advisory tier. The only support that
        # counts is evidence the pin was wrong -- not consistency with the
        # other panel.
        assert "SILENCE IS THE SAME CASE, NOT A LESSER ONE" in contract
        assert "never mentions it" in contract
        assert "does not even learn a decision was reversed" in contract
        assert "and so is a deleted pin the description never\n  mentions" in contract
        assert '"consistency", "symmetry" or "matches the other panel" is not it' in contract

    def test_an_unverified_premise_on_an_availability_path_is_the_block_case(self) -> None:
        # "When torn, choose CONCERNS" made BLOCK unreachable exactly where the
        # reviewer cannot verify the premise at all -- it has no shell and no
        # second platform -- so the tie-breaker is scoped to reversible cases
        # and this one is named as a trigger.
        contract = _fp_contract()
        assert "UNVERIFIED PREMISE ON A CORE\n  AVAILABILITY PATH" in contract
        assert "ONLY where being wrong is REVERSIBLE" in contract
        assert 'Here "unclear" is the BLOCK case, not the CONCERNS case' in contract
        assert "the author can" in contract
        assert "Do not soften this to a CONCERNS item" in contract
        # The carve-outs stay a CLOSED set -- now three: (a) availability
        # premise, (b) rider, (c) product shape without a recorded decision,
        # which is also this lane's only "cannot evaluate". An open-ended
        # fourth would put the tie-breaker back in charge of everything.
        assert "there is no fourth" in contract
        assert "The three exceptions are named at the" in contract
        assert "there is no third" not in contract
        assert "The single exception is the combination" not in contract

    def test_undeclared_and_rides_along_are_inventory_tags_not_verdicts(self) -> None:
        # ~50% of PRs drew CONCERNS, so the signal cost nothing to ignore.
        # These two tags print as inventory and do not carry the verdict by
        # themselves; CONCERNS is reserved for premise and depth risks.
        contract = _fp_contract()
        assert "`undeclared` and `rides along`\n  are INVENTORY TAGS ONLY" in contract
        assert "on\n  their own they do NOT reach CONCERNS" in contract
        assert "a harm-free rider is inventory" in contract
        # The tags themselves survive on the item line.
        assert "undeclared | rides" in contract
        # A premise risk carried BY the rider still reaches CONCERNS.
        assert "when the rider itself\n  carries one of the premise risks" in contract

    def test_output_leads_with_problems_and_drops_the_praise_punchline(self) -> None:
        contract = _fp_contract()
        # The PASS punchline does not argue the author's case.
        assert "why every item earns its place" not in contract.split("Output EXACTLY")[0]
        assert "Never\nexplain why every item earns its place" in contract
        assert "the ONE thing a human should still verify before merge" in contract
        assert "`Nothing to check.`" in contract
        # Non-justified items are read FIRST, above the inventory.
        assert "### Not justified as shipped" in contract
        assert contract.index("### Not justified as shipped") < contract.index(
            "### What this change ships"
        )
        # `justified` carries no reason -- the parenthetical was the praise.
        assert "`justified` is exactly that ONE word" in contract
        assert "Only a NON-justified tag carries a reason" in contract

    def test_the_inventory_is_collapsed_on_every_verdict(self) -> None:
        # The block was left EXPANDED on CONCERNS/BLOCK -- exactly the verdicts
        # a human opens the comment for -- so the findings sat under a full
        # list of the items that were fine. Every item a human must act on is
        # already under `### Not justified as shipped`, so the inventory is an
        # audit trail and stays one click away on every verdict, with the
        # counts in the summary line.
        contract = _fp_contract()
        assert "ALWAYS COLLAPSED, on every verdict" in contract
        assert "<details><summary>Inventory (N items) — M justified</summary>" in contract
        assert "</details>" in contract
        # The old conditional is gone in both directions.
        assert "WHEN EVERY ITEM IS TAGGED `justified`" not in contract
        assert "leave the block EXPANDED" not in contract
        # The inventory is still always emitted -- collapsing is not omitting.
        assert "ALWAYS present, even on PASS" in contract
        assert "A PASS here is a claim about EVERY item" in contract
        # The findings a human acts on live above the block, not in it.
        assert "this block is the audit trail, not the summary" in contract

    def test_every_finding_states_what_would_clear_it(self) -> None:
        # A finding with no statable resolution is what produced 31 of 57
        # unanswered CONCERNS: nothing told the author when they were done.
        contract = _fp_contract()
        # Once on the item entry, once on the Blocker -- the two places a
        # finding can appear -- and nowhere else, because there is nowhere else.
        assert (
            contract.count("`Clears when: <the concrete evidence or change that resolves it>`") == 2
        )
        assert "REQUIRED on every\nitem whose tag reaches CONCERNS" in contract
        assert "is not a finding -- retag it or drop it" in contract

    def test_the_output_diet_tightened_and_kept_its_machine_read_lines(self) -> None:
        contract = _fp_contract()
        assert "review under ~180 words excluding the inventory lines" in contract
        assert "~250 words" not in contract
        # The two lines the workflows grep for are untouched, and still first
        # and last in the emitted shape.
        assert "First-Principles-Verdict: <PASS | CONCERNS | BLOCK>" in contract
        assert contract.rstrip().endswith("[FIRST-PRINCIPLES-REVIEWED] <head sha>")
        for name in FP_LANES:
            workflow = _workflow(name)
            assert _reads_header_anchored(workflow, "First-Principles-Verdict:"), name
        # The subtraction-only stance and the SYSTEM RULES block stay.
        assert contract.startswith("SYSTEM RULES (non-negotiable")
        assert "EVERY suggestion you emit must be a SUBTRACTION" in contract
        # Security boundaries, repo context and the user-count rule stay.
        assert "REPO CONTEXT: Kiro Crew is an open-source AI agent platform" in contract
        assert "DO NOT REASON FROM AN ASSUMED USER COUNT, in either direction" in contract
        assert "the AGENT is untrusted with respect to its own governance" in contract

    def test_over_engineering_is_judged_against_the_frozen_goal(self) -> None:
        # The body below `## What changed` is regenerated each round to match
        # the diff, so it cannot be the Goal a mechanism is measured against.
        # An out-of-goal mechanism must surface as a removable Subtraction, and
        # an older PR without the frozen headings still has a Goal to read.
        flat = " ".join(_fp_contract().split())
        assert "THE GOAL: when the description has a `**Goal:**` line" in flat
        assert "never from `## What changed` or anything below it" in flat
        assert "When there is no `**Goal:**` line (an older PR)" in flat
        # Today's template already has `## Problem / Motivation`, so that
        # heading alone must not switch a PR into frozen-goal mode.
        assert "a `## Problem / Motivation` section or" not in flat
        assert "does any mechanism go beyond the Goal's scope" in flat
        assert "If so, is that justified?" in flat
        assert "MINIMALITY PRINCIPLE: the change fits the original Goal" in flat
        assert "tagged `oversized`" in flat
        assert "`Clears when: the mechanism is removed, or a human amends the Goal.`" in flat


class TestFirstPrinciplesOneStatementPerProblem:
    """A review that says the same three items three times -- under
    `### Not justified as shipped`, again under `### Watch`, again under
    `### Subtractions` -- runs to ~600 words against a 180-word cap and buries
    the finding under the sections that restate it; a line of the model's own
    narration above the verdict header adds noise at the top. Collapsing the
    inventory does not fix that: the restating sections are what the template
    asks for. So the item entry is the finding, its `Clears when:` and its
    `Subtraction:` together, there is no later section, and the workflow
    trims anything before the header and counts the words that remain."""

    def test_the_item_entry_is_the_only_place_a_problem_appears(self) -> None:
        contract = _fp_contract()
        shape = contract.split("Output EXACTLY this shape")[1]
        assert "that entry is the item's ONLY\nappearance outside the inventory" in contract
        assert "there is no later section that\nsays it again" in contract
        assert "Never write a Watch, Subtractions or Suggestions heading" in contract
        assert "a second section that restates them\nis what buried the finding" in contract
        # The two restating sections are gone from the emitted shape.
        assert "### Watch" not in shape
        assert "### Subtractions" not in shape
        # What survives: the problems, the collapsed audit trail, the blockers.
        assert (
            shape.index("### Not justified as shipped")
            < shape.index("### What this change ships")
            < shape.index("### Blockers")
        )
        # Both per-item lines are named as lines ON the entry, not sections.
        assert "`Clears when: <the concrete evidence or change that resolves it>` --" in shape
        assert "`Subtraction: <the exact symbol/field/file" in shape
        # A Blocker is evidence for an item already listed once, not a copy.
        assert (
            "the Blockers entry carries the evidence, not a\nsecond copy of the reason" in contract
        )

    def test_the_style_bans_narration_and_says_each_problem_once(self) -> None:
        contract = _fp_contract()
        assert "NO narration of your own process" in contract
        assert "the verdict header is the FIRST byte of your last\nmessage" in contract
        assert "Each problem is\nstated ONCE" in contract
        assert "the\nworkflow counts them and flags an overrun" in contract

    def test_the_rule_the_local_loop_parses_still_holds(self) -> None:
        # The kirocrew-prepare-pr loop reads items out of `### Not justified as shipped`
        # by bullet + continuation lines, so an entry shaped as the contract
        # now asks (bullet, then indented `Clears when:` / `Subtraction:`)
        # must yield ONE item carrying both lines, not three.
        mod = _review_contract_module()
        body = (
            "First-Principles-Verdict: CONCERNS\n\n"
            "**punchline**\n\n"
            "### Not justified as shipped\n"
            "- Item 4 — unjustified move: reinstates pills against SidePanel.tsx:1459.\n"
            "  Subtraction: defer `panelTabStyles.ts`; keep `.side-tab-active`.\n"
            "  Clears when: a linked report names who misread the fused tabs.\n"
            "- Item 5 — undeclared: hide controls relabelled to X, description silent.\n\n"
            "### What this change ships\n"
            "<details><summary>Inventory (2 items) — 0 justified</summary>\n"
            "1. pills — unjustified move\n2. X icon — undeclared\n</details>\n\n"
            "[FIRST-PRINCIPLES-REVIEWED] abc\n"
        )
        items = mod.design_section_items(body)
        assert [section for section, _ in items] == [
            "Not justified as shipped",
            "Not justified as shipped",
        ]
        first = items[0][1]
        assert "Clears when: a linked report" in first
        assert "Subtraction: defer `panelTabStyles.ts`" in first
        assert "Item 5" not in first
        # `Clears when:` is the entry's LAST line for a reason: CLEARS_WHEN_RE
        # runs on the collapsed item and reads to its end, so a line after it
        # would be swallowed into the clearance. The contract orders
        # `Subtraction:` first, and the extracted clearance stays clean.
        clears = mod.CLEARS_WHEN_RE.search(first)
        assert clears is not None
        assert clears.group(1).strip() == "a linked report names who misread the fused tabs."
        assert "Subtraction" not in clears.group(1)
        contract = _fp_contract()
        shape = contract.split("Output EXACTLY this shape")[1]
        assert shape.index("`Subtraction: <") < shape.index("`Clears when: <")
        assert "It is the\nLAST line of the entry" in contract
