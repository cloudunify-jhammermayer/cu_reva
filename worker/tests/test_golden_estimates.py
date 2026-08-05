"""Unit tests for the golden-estimate calibration file loader."""

import textwrap

from reva.golden_estimates import (
    COMPLEXITY_DRIVERS,
    DEFAULT_BANDS,
    MAX_DRIVERS_PER_STORY,
    GOLDEN_FILENAME,
    GoldenStory,
    apply_anchor,
    calibration_block,
    load,
    render,
    score,
)
from reva.types import StoryEstimate, TicketAnalysisResult, TicketIssueItem

from tests.conftest import SHIPPED_PROMPTS


def _write(tmp_path, body: str) -> str:
    (tmp_path / GOLDEN_FILENAME).write_text(textwrap.dedent(body))
    return str(tmp_path)


VALID = """
    version: 1
    bands:
      configuration: {min_hours: 0.5, max_hours: 1.5}
      small:         {min_hours: 1,   max_hours: 2}
      medium:        {min_hours: 2,   max_hours: 4}
      large:         {min_hours: 4,   max_hours: 8}
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

# The shape the file actually ships in: valid bands, zero anchors.
EMPTY_ANCHORS = """
    version: 1
    bands:
      configuration: {min_hours: 0.5, max_hours: 1.5}
      small:         {min_hours: 1,   max_hours: 2}
      medium:        {min_hours: 2,   max_hours: 4}
      large:         {min_hours: 4,   max_hours: 8}
    anchors: []
"""


def test_loads_valid_file(tmp_path):
    golden, degradations = load(_write(tmp_path, VALID))

    assert degradations == []
    assert len(golden.anchors) == 1
    assert golden.bands["medium"].min_hours == 2
    assert [s.id for s in golden.anchors[0].stories] == [
        "bom-copy-mechanism",
        "procurement-release",
    ]


def test_missing_file_falls_back_to_default_bands(tmp_path):
    golden, degradations = load(str(tmp_path))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 8
    assert [d.reason for d in degradations] == ["file_missing"]


def test_malformed_yaml_falls_back_to_default_bands(tmp_path):
    golden, degradations = load(_write(tmp_path, "bands: [unclosed\n"))

    assert golden.anchors == []
    assert golden.bands["configuration"].min_hours == 0.5
    assert [d.reason for d in degradations] == ["file_unreadable"]


def test_empty_file_is_malformed(tmp_path):
    golden, degradations = load(_write(tmp_path, ""))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 8
    assert [d.reason for d in degradations] == ["file_malformed"]


def test_malformed_bands_section_falls_back_to_default_bands(tmp_path):
    body = """
        version: 1
        bands: [oops]
    """
    golden, degradations = load(_write(tmp_path, body))

    assert golden.anchors == []
    assert golden.bands["large"].max_hours == 8
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


def test_resolve_tolerates_the_backticks_the_block_renders(tmp_path):
    """The rendered anchor list wraps every id in backticks, and the prompt
    tells the model to copy the id from there. A model that brings the
    formatting along must still resolve — otherwise every ref is nulled and
    the feature does nothing on its first live day."""
    golden, _ = load(_write(tmp_path, VALID))

    assert golden.resolve("`bom-copies#procurement-release`") is not None
    assert golden.resolve("  bom-copies#procurement-release \n") is not None
    assert golden.resolve("`` ") is None


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
    assert "0.5–1.5 h" in text
    assert "4–8 h" in text
    assert "`bom-copies#bom-copy-mechanism`" in text
    assert "Order-bound BoM copy mechanism" in text
    assert "6 h" in text
    assert "new_model, computed_logic" in text
    assert "10 h total" in text


def test_render_omits_retired_anchors(tmp_path):
    golden, _ = load(_write(tmp_path, VALID.replace("active: true", "active: false")))

    text, _ = render(golden)

    assert "bom-copies#bom-copy-mechanism" not in text
    assert "0.5–1.5 h" in text


def test_render_disabled_is_bands_only(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    text, degradations = render(golden, enabled=False)

    assert "bom-copies" not in text
    assert "0.5–1.5 h" in text
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


def test_render_is_deterministic_across_loads(tmp_path):
    """Two independent loads of the same file must render identical text —
    nothing in the path may iterate a set or an unordered mapping (the prompt
    cache hits only on byte-identical system text)."""
    first, _ = load(_write(tmp_path, VALID))
    second, _ = load(_write(tmp_path, VALID))

    assert render(first)[0] == render(second)[0]


def test_render_with_a_zero_limit_is_bands_only(tmp_path):
    """REVA_GOLDEN_ESTIMATE_LIMIT=0 reaches here. Emitting the anchor headers
    and the how-to-use section above an empty list would instruct the model to
    cite anchors it was never shown."""
    golden, _ = load(_write(tmp_path, VALID))

    text, degradations = render(golden, limit=0)

    assert "Reference anchors" not in text
    assert "Prefer these over the bands" not in text
    assert "How to use the anchors" not in text
    assert "bom-copies" not in text
    assert "0.5–1.5 h" in text  # bands still render
    assert [d.reason for d in degradations] == ["anchor_limit_exceeded"]
    assert degradations[0].detail == {"rendered": 0, "available": 2}


def test_render_with_no_anchors_still_includes_the_total_sanity_check(tmp_path):
    """Regression: with `anchors: []`, `render()` used to return before reaching
    `_HOW_TO_USE`, silently dropping the total-sanity check and reference
    example — guidance the live prompt had. Both are anchor-independent, so they
    must render unconditionally from `_PREAMBLE`. The 10% quoting buffer is the
    third such rule: it applies whether or not any anchor renders."""
    golden, _ = load(_write(tmp_path, EMPTY_ANCHORS))

    text, degradations = render(golden)

    assert degradations == []
    assert "10–18 h total" in text
    assert "geo-location module took 15 h" in text
    assert "10 % buffer" in text


def test_how_to_use_anchor_sanity_line_only_appears_with_anchors(tmp_path):
    """The OTHER sanity instruction — 'check the total against the anchors'
    ticket totals above' — genuinely depends on anchors existing (there is
    nothing to check the total against otherwise), so unlike the check above
    it stays gated on `active_pairs()` being non-empty."""
    empty_golden, _ = load(_write(tmp_path, EMPTY_ANCHORS))
    empty_text, _ = render(empty_golden)
    assert "against the anchors' ticket totals above" not in empty_text

    populated_golden, _ = load(_write(tmp_path, VALID))
    populated_text, _ = render(populated_golden)
    assert "against the anchors' ticket totals above" in populated_text


def test_calibration_block_loads_and_renders(tmp_path):
    text, degradations = calibration_block(_write(tmp_path, VALID))

    assert "`bom-copies#procurement-release`" in text
    assert degradations == []


def test_calibration_block_on_missing_file_still_returns_bands(tmp_path):
    text, degradations = calibration_block(str(tmp_path))

    assert "2–4 h" in text
    assert [d.reason for d in degradations] == ["file_missing"]


def _anchor(kind="custom_dev", drivers=("new_model", "computed_logic")):
    return GoldenStory(
        id="a", scope="s", kind=kind, hours=5, drivers=list(drivers)
    )


def test_score_identical_drivers_and_kind_is_high():
    assert score(["new_model", "computed_logic"], "custom_dev", _anchor()) == "high"


def test_score_both_driver_sets_empty_is_high():
    # An identical "nothing unusual" profile is a genuine match, and Jaccard is
    # undefined on two empty sets — this case must be spelled out, not divided.
    assert score([], "custom_dev", _anchor(drivers=())) == "high"


def test_score_two_of_three_overlap_is_high():
    # |A∩B| / |A∪B| = 2/3 = 0.66 >= 0.6
    assert score(
        ["new_model", "computed_logic"],
        "custom_dev",
        _anchor(drivers=("new_model", "computed_logic", "view_tweak")),
    ) == "high"


def test_score_one_of_three_overlap_is_medium():
    # 1/3 = 0.33, above zero but below the high threshold
    assert score(
        ["new_model"],
        "custom_dev",
        _anchor(drivers=("computed_logic", "view_tweak", "new_model")),
    ) == "medium"


def test_score_disjoint_drivers_is_low():
    assert score(["access_rights"], "custom_dev", _anchor()) == "low"


def test_score_kind_mismatch_is_low_even_on_identical_drivers():
    assert score(
        ["new_model", "computed_logic"], "configuration", _anchor()
    ) == "low"


def test_score_no_anchor_is_low():
    assert score(["new_model"], "custom_dev", None) == "low"


def test_score_one_side_empty_is_low():
    assert score([], "custom_dev", _anchor()) == "low"
    assert score(["new_model"], "custom_dev", _anchor(drivers=())) == "low"


def test_score_ignores_driver_order_and_duplicates():
    assert score(
        ["computed_logic", "new_model", "new_model"], "custom_dev", _anchor()
    ) == "high"


def _estimate(**kwargs):
    base = dict(story="s", kind="custom_dev", min_hours=1, max_hours=2)
    return StoryEstimate(**{**base, **kwargs})


def test_story_estimate_defaults_are_unanchored():
    est = _estimate()

    assert est.anchor_ref is None
    assert est.complexity_drivers == []
    assert est.anchor_confidence == "low"


def test_unknown_drivers_are_dropped_not_fatal():
    est = _estimate(complexity_drivers=["new_model", "teleportation"])

    assert est.complexity_drivers == ["new_model"]


def test_more_than_three_drivers_are_truncated():
    # Pydantic max_length cannot help here: reva/tool_schema.py strips maxItems
    # from the Anthropic schema, so the model can return any number.
    est = _estimate(
        complexity_drivers=[
            "new_model", "computed_logic", "view_tweak", "access_rights"
        ]
    )

    assert est.complexity_drivers == ["new_model", "computed_logic", "view_tweak"]


def test_non_list_int_drivers_becomes_empty_not_fatal():
    # Reachable via the escalated-CLI path, which has no tool schema
    # constraining the model's output at all.
    est = _estimate(complexity_drivers=5)

    assert est.complexity_drivers == []


def test_none_drivers_becomes_empty_not_fatal():
    est = _estimate(complexity_drivers=None)

    assert est.complexity_drivers == []


def test_comma_joined_string_drivers_becomes_empty_not_fatal():
    # Not valid JSON, so _unwrap_json_list raises — must be caught, not
    # salvaged into a single-element list.
    est = _estimate(complexity_drivers="new_model, computed_logic")

    assert est.complexity_drivers == []


def test_bare_word_string_drivers_becomes_empty_not_fatal():
    est = _estimate(complexity_drivers="new_model")

    assert est.complexity_drivers == []


def test_analysis_survives_a_story_with_unparseable_drivers():
    # The whole point: an analysis that already cost real money must not be
    # thrown away because one story's complexity_drivers arrived malformed.
    result = TicketAnalysisResult(
        summary="s",
        estimates=[_estimate(complexity_drivers="new_model, computed_logic")],
    )

    assert result.estimates[0].complexity_drivers == []


def test_apply_anchor_resolves_and_scores(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(
        anchor_ref="bom-copies#bom-copy-mechanism",
        complexity_drivers=["new_model", "computed_logic"],
        anchor_confidence="high",
    )

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert degradations == []
    assert est.anchor_ref == "bom-copies#bom-copy-mechanism"
    assert est.anchor_confidence == "high"


def test_apply_anchor_overwrites_a_model_supplied_confidence(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(
        anchor_ref="bom-copies#procurement-release",
        complexity_drivers=["access_rights"],
        anchor_confidence="high",
    )

    apply_anchor(est, golden, score_confidence=True)

    # Disjoint drivers: the model's "high" is not trusted.
    assert est.anchor_confidence == "low"


def test_apply_anchor_nulls_an_unresolvable_ref_and_degrades(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(anchor_ref="ghost#story", complexity_drivers=["new_model"])

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert est.anchor_ref is None
    assert est.anchor_confidence == "low"
    assert [d.reason for d in degradations] == ["anchor_ref_unresolved"]


def test_apply_anchor_normalizes_a_backticked_ref(tmp_path):
    """The stored ref is the canonical id, not whatever formatting the model
    copied along with it — the calibration view looks it up by that id."""
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(
        anchor_ref="`bom-copies#bom-copy-mechanism`",
        complexity_drivers=["new_model", "computed_logic"],
    )

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert degradations == []
    assert est.anchor_ref == "bom-copies#bom-copy-mechanism"
    assert est.anchor_confidence == "high"


def test_apply_anchor_truncates_a_runaway_ref_in_the_ops_detail(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(anchor_ref="x" * 5000)

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert len(degradations[0].detail["anchor_ref"]) == 300


def test_dropped_unknown_driver_is_reported_not_swallowed(tmp_path):
    """The validator has no db, so it parks what it threw away on the item and
    apply_anchor turns it into a degradation the caller ops-events. A silently
    unanchored estimate must not look like a well-anchored one."""
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(complexity_drivers=["new_model", "teleportation"])

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert est.complexity_drivers == ["new_model"]
    assert [d.reason for d in degradations] == ["drivers_dropped"]
    assert degradations[0].detail == {"dropped": ["teleportation"]}


def test_drivers_truncated_at_the_cap_are_reported(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(
        complexity_drivers=[
            "new_model", "computed_logic", "view_tweak", "access_rights"
        ]
    )

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert [d.reason for d in degradations] == ["drivers_dropped"]
    assert degradations[0].detail == {"dropped": ["access_rights"]}


def test_unparseable_drivers_are_reported(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    est = _estimate(complexity_drivers="new_model, computed_logic")

    degradations = apply_anchor(est, golden, score_confidence=True)

    assert est.complexity_drivers == []
    assert [d.reason for d in degradations] == ["drivers_dropped"]
    assert degradations[0].detail == {"dropped": ["new_model, computed_logic"]}


def test_clean_drivers_report_nothing_when_nothing_was_dropped(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))

    assert apply_anchor(_estimate(), golden, score_confidence=True) == []
    assert apply_anchor(
        _estimate(complexity_drivers=["new_model"]), golden, score_confidence=True
    ) == []
    assert apply_anchor(
        _estimate(complexity_drivers=None), golden, score_confidence=True
    ) == []


def test_dropped_drivers_are_bookkeeping_and_never_persist():
    est = _estimate(complexity_drivers=["teleportation"])

    assert est.dropped_drivers == ["teleportation"]
    assert "dropped_drivers" not in est.model_dump()
    assert "dropped_drivers" not in TicketIssueItem(
        title="t", body="b", complexity_drivers=["teleportation"]
    ).model_dump()


def test_a_model_supplied_dropped_drivers_value_cannot_be_faked():
    est = _estimate(complexity_drivers=["new_model"], dropped_drivers=["invented"])

    assert est.dropped_drivers == []


def test_dropped_drivers_on_an_issue_are_reported_too(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    issue = TicketIssueItem(title="t", body="b", complexity_drivers=["teleportation"])

    degradations = apply_anchor(issue, golden, score_confidence=False)

    assert [d.reason for d in degradations] == ["drivers_dropped"]


def test_apply_anchor_on_an_issue_skips_confidence(tmp_path):
    golden, _ = load(_write(tmp_path, VALID))
    issue = TicketIssueItem(
        title="t",
        body="b",
        anchor_ref="bom-copies#bom-copy-mechanism",
        complexity_drivers=["new_model"],
    )

    degradations = apply_anchor(issue, golden, score_confidence=False)

    assert degradations == []
    assert issue.anchor_ref == "bom-copies#bom-copy-mechanism"
    assert not hasattr(issue, "anchor_confidence")


def test_shipped_file_loads_cleanly():
    """The file that actually ships must parse with zero degradations."""
    golden, degradations = load(SHIPPED_PROMPTS)

    assert degradations == []
    assert set(golden.bands) == set(DEFAULT_BANDS)


def test_shipped_file_matches_the_bands_in_code():
    golden, _ = load(SHIPPED_PROMPTS)

    for name, (lo, hi) in DEFAULT_BANDS.items():
        assert (golden.bands[name].min_hours, golden.bands[name].max_hours) == (lo, hi)


def test_shipped_calibration_block_includes_the_total_sanity_check():
    """Direct regression for the shipped file — the exact call the live prompt
    makes. The total-sanity check, the reference example and the 10% quoting
    buffer are anchor-independent and must survive it."""
    text, degradations = calibration_block(SHIPPED_PROMPTS)

    assert degradations == []
    assert "10–18 h total" in text
    assert "10 % buffer" in text


def test_how_to_use_text_fits_both_consumers(tmp_path):
    """One renderer feeds both the analysis prompt (stories, which have
    `assumptions`) and the issue planner (issues, which do NOT — and whose
    tool schema is strict with additionalProperties: false). The shared text
    may name neither the unit nor a field only one of them has."""
    golden, _ = load(_write(tmp_path, VALID))

    text, _ = render(golden)

    assert "`assumptions`" not in text
    assert "For each item you estimate" in text
    assert "must never contain backticks" in text
