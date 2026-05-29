"""HTML formatter for TicketAnalysisResult — converts structured Claude output
to an HTML string suitable for Odoo HTML fields.

Kept separate from ticket_analyzer.py so the Claude orchestration and the
presentation layer can change independently.
"""

from __future__ import annotations

from reva.types import TicketAnalysisResult

_CATEGORY_LABEL = {
    "happy_path": "Happy Path",
    "edge_case": "Edge Cases",
    "error_scenario": "Error Scenarios",
}

_MISSING_BADGE: dict[str, str] = {
    "certain": (
        '<span style="font-size:0.75em;font-weight:bold;color:#cf222e;'
        'background:#fff0ee;border:1px solid #ff8182;border-radius:3px;'
        'padding:1px 5px;margin-right:6px;vertical-align:middle;">certain</span>'
    ),
    "likely": (
        '<span style="font-size:0.75em;font-weight:bold;color:#9a6700;'
        'background:#fff8c5;border:1px solid #e3b341;border-radius:3px;'
        'padding:1px 5px;margin-right:6px;vertical-align:middle;">likely</span>'
    ),
    "possible": (
        '<span style="font-size:0.75em;font-weight:bold;color:#57606a;'
        'background:#f6f8fa;border:1px solid #d0d7de;border-radius:3px;'
        'padding:1px 5px;margin-right:6px;vertical-align:middle;">possible</span>'
    ),
}

_CONFIDENCE_BADGE: dict[str, str] = {
    "explicit": (
        '<span style="font-size:0.75em;font-weight:bold;color:#1a7f37;'
        'background:#dafbe1;border:1px solid #82cfae;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">from ticket</span>'
    ),
    "inferred": (
        '<span style="font-size:0.75em;font-weight:bold;color:#9a6700;'
        'background:#fff8c5;border:1px solid #e3b341;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">inferred</span>'
    ),
    "assumed": (
        '<span style="font-size:0.75em;font-weight:bold;color:#57606a;'
        'background:#f6f8fa;border:1px solid #d0d7de;border-radius:3px;'
        'padding:1px 5px;margin-left:6px;vertical-align:middle;">assumed</span>'
    ),
}


def format_ticket_html(result: TicketAnalysisResult) -> str:
    """Convert a TicketAnalysisResult into an HTML string suitable for Odoo HTML fields."""
    parts: list[str] = []

    # Stats banner
    stat_parts = []
    if result.missing_info:
        n_certain = sum(1 for i in result.missing_info if i.confidence == "certain")
        gap_str = f"{len(result.missing_info)} gap{'s' if len(result.missing_info) != 1 else ''}"
        if n_certain:
            gap_str += f" ({n_certain} certain)"
        stat_parts.append(gap_str)
    if result.acceptance_criteria:
        n = len(result.acceptance_criteria)
        stat_parts.append(f"{n} acceptance criteri{'a' if n != 1 else 'on'}")
    if result.test_cases:
        n = len(result.test_cases)
        stat_parts.append(f"{n} test case{'s' if n != 1 else ''}")
    if result.definition_of_ready:
        n = len(result.definition_of_ready)
        stat_parts.append(f"{n} DoR item{'s' if n != 1 else ''}")
    if result.definition_of_done:
        n = len(result.definition_of_done)
        stat_parts.append(f"{n} DoD item{'s' if n != 1 else ''}")
    if stat_parts:
        parts.append(
            '<p style="font-size:0.85em;font-weight:bold;color:#24292f;'
            'border-bottom:1px solid #d0d7de;padding-bottom:6px;margin-bottom:4px;">'
            + " &nbsp;&middot;&nbsp; ".join(stat_parts)
            + "</p>"
        )

    # Legend
    sourced = "".join(
        f'<span style="margin-right:10px;">{_CONFIDENCE_BADGE[k]} {label}</span>'
        for k, label in [
            ("explicit", "stated in ticket"),
            ("inferred", "derived from context"),
            ("assumed", "standard practice"),
        ]
    )
    missing = "".join(
        f'<span style="margin-right:10px;">{_MISSING_BADGE[k]} {label}</span>'
        for k, label in [
            ("certain", "definitely missing"),
            ("likely", "probably missing"),
            ("possible", "possibly missing"),
        ]
    )
    parts.append(
        f'<p style="font-size:0.8em;color:#57606a;border-bottom:1px solid #d0d7de;padding-bottom:6px;">'
        f'<strong>Requirements:</strong> {sourced}&nbsp;&nbsp;'
        f'<strong>Gaps:</strong> {missing}</p>'
    )

    # Summary
    parts.append(f"<h2>Summary</h2><p>{_esc(result.summary)}</p>")

    # Missing information
    if result.missing_info:
        items = "".join(
            f"<li>{_MISSING_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.missing_info
        )
        parts.append(f"<h2>Missing Information</h2><ul>{items}</ul>")
    else:
        parts.append("<h2>Missing Information</h2><p><em>No missing information identified.</em></p>")

    # Acceptance criteria
    if result.acceptance_criteria:
        items = "".join(
            f"<li>"
            f"{_CONFIDENCE_BADGE.get(ac.confidence, '')} "
            f"<strong>Given</strong> {_esc(ac.given)} "
            f"<strong>When</strong> {_esc(ac.when)} "
            f"<strong>Then</strong> {_esc(ac.then)}"
            f"</li>"
            for ac in result.acceptance_criteria
        )
        parts.append(f"<h2>Acceptance Criteria</h2><ul>{items}</ul>")

    # Test cases grouped by category
    if result.test_cases:
        by_category: dict[str, list[tuple[str, str]]] = {}
        for tc in result.test_cases:
            by_category.setdefault(tc.category, []).append((tc.description, tc.confidence))

        tc_html = "<h2>Test Cases</h2>"
        for cat in ("happy_path", "edge_case", "error_scenario"):
            cases = by_category.get(cat, [])
            if cases:
                label = _CATEGORY_LABEL[cat]
                items = "".join(
                    f"<li>{_CONFIDENCE_BADGE.get(conf, '')} {_esc(desc)}</li>"
                    for desc, conf in cases
                )
                tc_html += f"<h3>{label}</h3><ul>{items}</ul>"
        parts.append(tc_html)

    # Definition of ready
    if result.definition_of_ready:
        items = "".join(
            f"<li>&#9744; {_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.definition_of_ready
        )
        parts.append(f"<h2>Definition of Ready</h2><ul>{items}</ul>")

    # Definition of done
    if result.definition_of_done:
        items = "".join(
            f"<li>&#9744; {_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.definition_of_done
        )
        parts.append(f"<h2>Definition of Done</h2><ul>{items}</ul>")

    # Odoo-specific notes
    if result.odoo_notes:
        items = "".join(
            f"<li>{_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.odoo_notes
        )
        parts.append(f"<h2>Odoo-Specific Notes</h2><ul>{items}</ul>")

    parts.append("<p><em>Generated by REVA</em></p>")
    return "\n".join(parts)


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
