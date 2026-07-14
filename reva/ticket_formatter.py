"""HTML formatter for TicketAnalysisResult — converts structured Claude output
to an HTML string suitable for Odoo HTML fields.

Kept separate from ticket_analyzer.py so the Claude orchestration and the
presentation layer can change independently.
"""

from __future__ import annotations

from reva.types import TicketAnalysisResult

_ESTIMATE_KIND_LABEL = {
    "custom_dev": "custom development",
    "configuration": "configuration",
    "mixed": "mixed",
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
    if result.estimates:
        total_min = sum(e.min_hours for e in result.estimates)
        total_max = sum(e.max_hours for e in result.estimates)
        stat_parts.append(f"est. {_fmt_hours(total_min)}–{_fmt_hours(total_max)}h")
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

    # Odoo-specific notes
    if result.odoo_notes:
        items = "".join(
            f"<li>{_CONFIDENCE_BADGE.get(i.confidence, '')} {_esc(i.text)}</li>"
            for i in result.odoo_notes
        )
        parts.append(f"<h2>Odoo-Specific Notes</h2><ul>{items}</ul>")

    sc = result.standard_coverage
    if sc.coverage != "unknown" or sc.features:
        parts.append("<h2>Standard Odoo Coverage</h2>")
        parts.append(f"<p><strong>Coverage:</strong> {_esc(sc.coverage)}</p>")
        if sc.features:
            items = []
            for feature in sc.features:
                bits = [f"<strong>{_esc(feature.name)}</strong>"]
                if feature.module:
                    bits.append(f"({_esc(feature.module)}, {_esc(feature.kind)})")
                if feature.how:
                    bits.append(f"- {_esc(feature.how)}")
                if feature.reference:
                    bits.append(f"<em>[{_esc(feature.reference)}]</em>")
                bits.append(f"<small>confidence: {_esc(feature.confidence)}</small>")
                items.append("<li>" + " ".join(bits) + "</li>")
            parts.append("<ul>" + "".join(items) + "</ul>")
        if sc.notes:
            parts.append(f"<p>{_esc(sc.notes)}</p>")

    ec = result.existing_customizations
    if ec.coverage != "unknown" or ec.features:
        parts.append("<h2>Existing Customizations</h2>")
        parts.append(f"<p><strong>Coverage:</strong> {_esc(ec.coverage)}</p>")
        if ec.features:
            items = []
            for feature in ec.features:
                bits = [f"<strong>{_esc(feature.name)}</strong>"]
                if feature.addon:
                    bits.append(f"({_esc(feature.addon)})")
                if feature.how:
                    bits.append(f"- {_esc(feature.how)}")
                if feature.reference:
                    bits.append(f"<em>[{_esc(feature.reference)}]</em>")
                bits.append(f"<small>confidence: {_esc(feature.confidence)}</small>")
                items.append("<li>" + " ".join(bits) + "</li>")
            parts.append("<ul>" + "".join(items) + "</ul>")
        if ec.notes:
            parts.append(f"<p>{_esc(ec.notes)}</p>")

    # Development estimate
    if result.estimates:
        total_min = sum(e.min_hours for e in result.estimates)
        total_max = sum(e.max_hours for e in result.estimates)
        items = []
        for e in result.estimates:
            line = (
                f"{_esc(e.story)} — "
                f"<strong>{_fmt_hours(e.min_hours)}–{_fmt_hours(e.max_hours)} h</strong> "
                f"<small>{_esc(_ESTIMATE_KIND_LABEL.get(e.kind, e.kind))}"
                f" &middot; confidence: {_esc(e.confidence)}</small>"
            )
            if e.assumptions:
                assumptions = "; ".join(_esc(a) for a in e.assumptions)
                line += f"<br><small>Assumptions: {assumptions}</small>"
            items.append(f"<li>{line}</li>")
        parts.append(
            f"<h2>Development Estimate</h2><ul>{''.join(items)}</ul>"
            f"<p><strong>Total: {_fmt_hours(total_min)}–{_fmt_hours(total_max)} h</strong> "
            f"<small>(mid-level Odoo developer, AI-assisted; implementation + developer "
            f"testing only)</small></p>"
        )

    parts.append("<p><em>Generated by REVA</em></p>")
    return "\n".join(parts)


def _fmt_hours(hours: float) -> str:
    """Render an hour value without a trailing '.0' (e.g. 4.0 -> '4', 4.5 -> '4.5')."""
    return f"{hours:g}"


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
