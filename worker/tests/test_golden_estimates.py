"""Unit tests for the golden-estimate calibration file loader."""

import textwrap

from reva.golden_estimates import (
    COMPLEXITY_DRIVERS,
    MAX_DRIVERS_PER_STORY,
    GOLDEN_FILENAME,
    calibration_block,
    load,
    render,
)


def _write(tmp_path, body: str) -> str:
    (tmp_path / GOLDEN_FILENAME).write_text(textwrap.dedent(body))
    return str(tmp_path)


VALID = """
    version: 1
    bands:
      configuration: {min_hours: 0.5, max_hours: 2}
      small:         {min_hours: 1,   max_hours: 4}
      medium:        {min_hours: 3,   max_hours: 8}
      large:         {min_hours: 6,   max_hours: 12}
    anchors:
      - id: bom-copies
        ticket: "BoM copies + procurement release"
        total_hours: 10
        active: true
        stories:
          - id: bom-copy-mechanism
            scope: "Order-bound BoM copy mechanism"
            kind: custom_dev
            hours: 6
            drivers: [new_model, computed_logic]
          - id: procurement-release
            scope: "Selective procurement release"
            kind: custom_dev
            hours: 4
            drivers: [cross_module_workflow]
"""


def test_loads_valid_file(tmp_path):
    golden, degradations = load(_write(tmp_path, VALID))

    assert degradations == []
    assert len(golden.anchors) == 1
    assert golden.bands["medium"].min_hours == 3
    assert [s.id for s in golden.anchors[0].stories] == [
        "bom-copy-mechanism",
        "procurement-release",
    ]


def test_missing_file_falls_back_to_default_bands(tmp_path):
    golden, degradations = load(str(tmp_path))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 12
    assert [d.reason for d in degradations] == ["file_missing"]


def test_malformed_yaml_falls_back_to_default_bands(tmp_path):
    golden, degradations = load(_write(tmp_path, "bands: [unclosed\n"))

    assert golden.anchors == []
    assert golden.bands["configuration"].min_hours == 0.5
    assert [d.reason for d in degradations] == ["file_unreadable"]


def test_empty_file_is_malformed(tmp_path):
    golden, degradations = load(_write(tmp_path, ""))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 12
    assert [d.reason for d in degradations] == ["file_malformed"]


def test_malformed_bands_section_falls_back_to_default_bands(tmp_path):
    body = """
        version: 1
        bands: [oops]
    """
    golden, degradations = load(_write(tmp_path, body))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 12
    assert [d.reason for d in degradations] == ["bands_invalid"]


def test_invalid_anchor_is_dropped_and_the_rest_load(tmp_path):
    body = VALID + """
      - id: BAD_SLUG
        ticket: "Uppercase id is not a slug"
        total_hours: 3
        stories:
          - id: only-story
            scope: "Something"
            kind: custom_dev
            hours: 3
            drivers: []
"""
    golden, degradations = load(_write(tmp_path, body))

    assert [a.id for a in golden.anchors] == ["bom-copies"]
    assert [d.reason for d in degradations] == ["anchor_invalid"]


def test_unknown_driver_invalidates_its_anchor(tmp_path):
    body = VALID.replace("[cross_module_workflow]", "[teleportation]")
    golden, degradations = load(_write(tmp_path, body))

    assert golden.anchors == []
    assert [d.reason for d in degradations] == ["anchor_invalid"]


def test_more_than_three_drivers_invalidates_its_anchor(tmp_path):
    body = VALID.replace(
        "[new_model, computed_logic]",
        "[new_model, computed_logic, view_tweak, access_rights]",
    )
    _, degradations = load(_write(tmp_path, body))

    assert [d.reason for d in degradations] == ["anchor_invalid"]
    assert MAX_DRIVERS_PER_STORY == 3


def test_duplicate_anchor_id_drops_the_second(tmp_path):
    golden, degradations = load(_write(tmp_path, VALID + VALID.split("anchors:")[1]))

    assert len(golden.anchors) == 1
    assert [d.reason for d in degradations] == ["anchor_invalid"]


def test_duplicate_story_id_within_an_anchor_is_invalid(tmp_path):
    body = VALID.replace("id: procurement-release", "id: bom-copy-mechanism")
    _, degradations = load(_write(tmp_path, body))

    assert [d.reason for d in degradations] == ["anchor_invalid"]


def test_total_hours_far_from_story_sum_degrades_but_still_loads(tmp_path):
    golden, degradations = load(_write(tmp_path, VALID.replace("total_hours: 10", "total_hours: 40")))

    assert len(golden.anchors) == 1
    assert [d.reason for d in degradations] == ["anchor_hours_mismatch"]


def test_resolve_finds_active_and_retired_stories(tmp_path):
    golden, _ = load(_write(tmp_path, VALID.replace("active: true", "active: false")))

    story = golden.resolve("bom-copies#procurement-release")

    assert story is not None and story.hours == 4
    assert golden.resolve("bom-copies#nope") is None
    assert golden.resolve("garbage") is None


def test_active_pairs_excludes_retired_anchors(tmp_path):
    active, _ = load(_write(tmp_path, VALID))
    retired, _ = load(_write(tmp_path, VALID.replace("active: true", "active: false")))

    assert len(active.active_pairs()) == 2
    assert retired.active_pairs() == []


def test_driver_enum_is_the_agreed_ten():
    assert COMPLEXITY_DRIVERS == (
        "data_migration",
        "cross_module_workflow",
        "new_model",
        "report_layout",
        "external_integration",
        "access_rights",
        "wizard_ui",
        "computed_logic",
        "scheduled_job",
        "view_tweak",
    )


def test_render_includes_bands_and_active_anchor_stories(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    text, degradations = render(golden)

    assert degradations == []
    assert "0.5–2 h" in text
    assert "6–12 h" in text
    assert "`bom-copies#bom-copy-mechanism`" in text
    assert "Order-bound BoM copy mechanism" in text
    assert "6 h" in text
    assert "new_model, computed_logic" in text
    assert "10 h total" in text


def test_render_omits_retired_anchors(tmp_path):
    golden, _ = load(_write(tmp_path, VALID.replace("active: true", "active: false")))

    text, _ = render(golden)

    assert "bom-copies#bom-copy-mechanism" not in text
    assert "0.5–2 h" in text


def test_render_disabled_is_bands_only(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    text, degradations = render(golden, enabled=False)

    assert "bom-copies" not in text
    assert "0.5–2 h" in text
    assert degradations == []


def test_render_caps_at_limit_and_degrades(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    text, degradations = render(golden, limit=1)

    assert "bom-copies#bom-copy-mechanism" in text
    assert "bom-copies#procurement-release" not in text
    assert [d.reason for d in degradations] == ["anchor_limit_exceeded"]


def test_render_lists_the_driver_enum_for_the_model(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    text, _ = render(golden)

    for driver in COMPLEXITY_DRIVERS:
        assert driver in text


def test_render_is_deterministic(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    assert render(golden)[0] == render(golden)[0]


def test_calibration_block_loads_and_renders(tmp_path):
    text, degradations = calibration_block(_write(tmp_path, VALID))

    assert "`bom-copies#procurement-release`" in text
    assert degradations == []


def test_calibration_block_on_missing_file_still_returns_bands(tmp_path):
    text, degradations = calibration_block(str(tmp_path))

    assert "3–8 h" in text
    assert [d.reason for d in degradations] == ["file_missing"]
