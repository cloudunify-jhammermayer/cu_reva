"""Deterministic contract generation and self-check."""

from __future__ import annotations

import json

from reva.odoo_contracts import CONTRACTS, check, generate


def test_generate_writes_manifest_schema_sample(tmp_path):
    version = generate(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["contracts_version"] == version
    names = {entry["name"] for entry in manifest["contracts"]}
    assert "tickets.write-field" in names
    assert "ticket-analysis" in names
    assert "hr.timesheet-results" in names
    entry = next(item for item in manifest["contracts"] if item["name"] == "tickets.write-field")
    assert entry["path"] == "/tickets/write-field"
    schema = json.loads((tmp_path / entry["schema"]).read_text())
    assert schema["properties"].keys() >= {"ticket_id", "model_name", "field_name", "html"}
    sample = json.loads((tmp_path / entry["sample"]).read_text())
    assert sample["ticket_id"] == 123


def test_generate_is_deterministic(tmp_path):
    version_a = generate(tmp_path / "a")
    version_b = generate(tmp_path / "b")
    assert version_a == version_b
    files = sorted(path.relative_to(tmp_path / "a") for path in (tmp_path / "a").rglob("*.json"))
    for rel in files:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_check_flags_drift(tmp_path):
    generate(tmp_path)
    assert check(tmp_path) == []
    (tmp_path / "manifest.json").write_text("{}")
    assert check(tmp_path)


def test_every_contract_has_schema_or_shape(tmp_path):
    generate(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["contracts"]) == len(CONTRACTS)
