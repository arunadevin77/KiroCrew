"""No built-in dashboard page may put a value into the DOM as markup.

Every crewmate's Dashboard tab and every root session's Dashboard view runs one of
these pages with scripts on. A page reads fold values and agent-written fields, and
those carry text a person did not write: task titles, summaries, issue text. Through
an HTML sink such text becomes elements and script. Through ``textContent`` it stays
text.

The crew panel templates carry the same rule
(``test_agent_panel_store.py::test_no_shipped_template_builds_markup_from_data``).
This is that rule for the dashboard pages, asserted on the source because it is a
rule about how a page is written, not about one payload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kiro_crew.dashboard_templates as dashboard_templates

BUILTIN = Path(dashboard_templates.__file__).resolve().parent / "builtin"

#: Every DOM API that parses a string as HTML. ``document.write`` also covers
#: ``document.writeln``.
HTML_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "createContextualFragment",
    "parseFromString",
    "setHTMLUnsafe",
    "parseHTMLUnsafe",
    "srcdoc",
)

PAGES = sorted(BUILTIN.glob("*/template.html"))


def test_the_scan_finds_the_shipped_pages() -> None:
    """An empty glob would pass every case below, so the scan must see pages."""
    assert len(PAGES) >= 12, f"expected the built-in pages under {BUILTIN}, found {PAGES}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.parent.name)
def test_no_builtin_page_uses_an_html_sink(page: Path) -> None:
    source = page.read_text(encoding="utf-8")
    for sink in HTML_SINKS:
        assert sink not in source, (
            f"{page.parent.name}/template.html reaches the DOM through {sink}; "
            "write values with textContent instead"
        )
