"""Review-workflow tests for the UX lanes: lens 0, which both UX lanes must carry
identically.
"""

from __future__ import annotations

from ai_review_workflows_helpers import (
    UX_LANES,
    _flat,
    _workflow,
)


class TestUxLensZeroIsIdenticalInBothLanes:
    """Lens 0 (product coherence) is where the UX lane judges look, information
    architecture, element economy and, since the placement check joined it,
    whether a control sits on the page a user would open to find it. The fork
    lane is the copy that reviews an outside contributor's PR, so a rule that
    lives in one copy only is a rule that does not apply to the PRs it was
    written for. Both copies are pinned to each other, not to a literal, so a
    deliberate rewording lands in both or fails here.
    """

    FIRST = "0. PRODUCT COHERENCE"
    LAST = "1. FIRST-TIME COMPREHENSION"

    def _lens_zero(self, workflow: str) -> str:
        lines = _workflow(workflow).splitlines()
        start = next((i for i, line in enumerate(lines) if self.FIRST in line), None)
        assert start is not None, f"{workflow} carries no lens 0"
        end = next(i for i, line in enumerate(lines[start:], start) if self.LAST in line)
        block = lines[start:end]
        indent = len(block[0]) - len(block[0].lstrip())
        return "\n".join(line[indent:] if line.strip() else "" for line in block)

    def test_both_ux_lanes_carry_an_identical_lens_zero(self) -> None:
        blocks = {name: self._lens_zero(name) for name in UX_LANES}
        reference = blocks[UX_LANES[0]]
        for name, block in blocks.items():
            assert (
                block == reference
            ), f"{name} lens 0 drifted from {UX_LANES[0]}; both UX lanes must carry the same text"

    def test_lens_zero_judges_placement_across_the_whole_app(self) -> None:
        for name in UX_LANES:
            flat = _flat(self._lens_zero(name))
            assert "- PLACEMENT" in flat, name
            # Judged where a user would look, across the app, not inside the
            # one panel the screenshot shows.
            assert "where a user LOOKING FOR IT would go first" in flat, name
            assert "across the whole app, not one panel" in flat, name
            # "The issue asked for it here" is not a design decision.
            assert "is NOT a design decision and is itself a finding" in flat, name
