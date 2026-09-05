"""Parser and renderer for the customer repos' release logs (spec 2026-09-04)."""

from __future__ import annotations

import pytest

from reva.release_log import (
    ReleaseLogParseError,
    parse_release_log,
    render_entry_html,
)

LOG = """---
release: lollipop
status: open
date: 2026-09-30
---

# Release Lollipop

Was mit diesem Release kommt, je Ticket.

## 7595 — Hintergrundjob-Warteschlange

- Status: umgesetzt
- Module: cu_queue 19.0.1.2.1

### Gebaut

Importe und andere lange Arbeiten laufen als Hintergrundjobs über den Odoo-Zeitplaner statt im Browser: automatische
Wiederholung bei vorübergehenden Fehlern. Eigene App „Job Queue“ mit Wiederholen & Abbrechen.

Erledigte Jobs werden nach 30 Tagen aufgeräumt.

### To-do

- Geplante Aktion „Job Queue: run background jobs“ einmal prüfen, sie ist standardmäßig aktiv _(Einstellungen →
  Technisch → Automatisierung → Geplante Aktionen)_
- Zeitwerte nur bei Bedarf anpassen: **Requeue** nach 15 min _(Job Queue → Konfiguration)_

## 6965 - Kostenstellen je Mandant

- Status: weitgehend
- Module: cu_import_tools 19.0.4.4.0 · cu_queue 19.0.1.2.1

### Gebaut

- „In Mandanten duplizieren“ legt je Mandant eine Kopie an
- Import liest die Excel-Vorlage in zwei Modi; Zellen wie `A<B` bleiben Text

## Entscheidungen

- **Kontenzusammenführung:** Variante 1 bis 4.

## Nicht in diesem Release

- Skonto.
"""


def test_frontmatter_and_entries_are_parsed():
    doc = parse_release_log(LOG)
    assert (doc.release, doc.status, doc.date) == ("lollipop", "open", "2026-09-30")
    assert sorted(doc.entries) == [6965, 7595]
    entry = doc.entries[7595]
    assert entry.title == "Hintergrundjob-Warteschlange"
    assert entry.status == "umgesetzt"
    assert entry.modules == ("cu_queue 19.0.1.2.1",)
    assert entry.built.startswith("Importe und andere lange Arbeiten")
    assert "Erledigte Jobs" in entry.built
    assert entry.todo.startswith("- Geplante Aktion")


def test_hyphen_headings_and_several_modules():
    entry = parse_release_log(LOG).entries[6965]
    assert entry.title == "Kostenstellen je Mandant"
    assert entry.modules == ("cu_import_tools 19.0.4.4.0", "cu_queue 19.0.1.2.1")
    assert entry.todo == ""


def test_trailing_sections_are_not_entries():
    doc = parse_release_log(LOG)
    assert all(isinstance(k, int) for k in doc.entries)
    assert "Kontenzusammenführung" not in doc.entries[6965].built


def test_frontmatter_comments_are_ignored():
    text = "---\nrelease: marshmallow\nstatus: open          # open | frozen | shipped\ndate: 2026-10-30\n---\n# R\n"
    doc = parse_release_log(text)
    assert (doc.release, doc.status, doc.date) == ("marshmallow", "open", "2026-10-30")
    assert doc.entries == {}


def test_missing_frontmatter_or_release_raises():
    with pytest.raises(ReleaseLogParseError):
        parse_release_log("# Release X\n\n## 1 — a\n")
    with pytest.raises(ReleaseLogParseError):
        parse_release_log("---\nstatus: open\n---\n# R\n")


def test_render_paragraphs_lists_and_inline_marks():
    entry = parse_release_log(LOG).entries[7595]
    html = render_entry_html(entry)
    assert html.startswith("<p><strong>Gebaut</strong></p><p>Importe und andere lange Arbeiten")
    assert "Wiederholen &amp; Abbrechen.</p><p>Erledigte Jobs werden nach 30 Tagen aufgeräumt.</p>" in html
    assert "<p><strong>To-do</strong></p><ul><li>Geplante Aktion" in html
    assert "<em>(Einstellungen → Technisch → Automatisierung → Geplante Aktionen)</em></li>" in html
    assert "<strong>Requeue</strong> nach 15 min <em>(Job Queue → Konfiguration)</em></li></ul>" in html
    assert "\n" not in html


def test_render_list_built_and_escaping_and_no_todo_section():
    entry = parse_release_log(LOG).entries[6965]
    html = render_entry_html(entry)
    assert html.startswith("<p><strong>Gebaut</strong></p><ul><li>„In Mandanten duplizieren“")
    assert "Zellen wie <code>A&lt;B</code> bleiben Text</li></ul>" in html
    assert "To-do" not in html


def test_render_empty_entry_is_empty():
    from reva.release_log import ReleaseEntry

    assert render_entry_html(ReleaseEntry(1, "t", "", (), "", "")) == ""
