"""Operator-authored calibration anchors for development-time estimates.

`prompts/golden_estimates.yml` is the single calibration source for all three
estimating prompts (ticket analysis, the escalated-CLI ticket skill, and the
issue planner), replacing three hand-maintained copies of the same four bands.

An anchor is a real closed ticket with its story breakdown, so it anchors both
levels the prompts estimate at: per-story hours, and the total-sanity check.

The file is authored and deployed by the operator, at the same trust level as
the prompts around it — it is NOT nonce-fenced. See the spec for why fencing it
would be actively harmful on the CLI path.

`load()` returns its degradations instead of recording them: it stays a pure
function that unit-tests without a database, and the callers already hold
`ctx.db` to map each one onto `writers.record_ops_event`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal, NamedTuple

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

GOLDEN_FILENAME = "golden_estimates.yml"

# Fixed enum. Values outside it are rejected — for file entries by dropping the
# entry (a typo to fix), for model output by dropping the value (re-running an
# analysis costs real money). Cheap to revise: nothing in the database is keyed
# on it.
COMPLEXITY_DRIVERS: tuple[str, ...] = (
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

MAX_DRIVERS_PER_STORY = 3

# Today's bands, kept in code as the fallback when the file is missing or its
# bands section is unusable. The file is the source of truth when it loads.
DEFAULT_BANDS: dict[str, tuple[float, float]] = {
    "configuration": (0.5, 2),
    "small": (1, 4),
    "medium": (3, 8),
    "large": (6, 12),
}

# A story's hours may legitimately not sum exactly to the ticket total, but a
# large gap means a typo in hand-entered hours, which silently distorts the
# total-sanity anchor.
_HOURS_MISMATCH_TOLERANCE = 0.25

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

StoryKind = Literal["custom_dev", "configuration", "mixed"]


class Degradation(NamedTuple):
    """One problem found while loading or rendering. The caller ops-events it."""

    reason: str
    detail: dict


class Band(BaseModel):
    min_hours: float = Field(gt=0)
    max_hours: float = Field(gt=0)

    @field_validator("max_hours")
    @classmethod
    def _max_not_below_min(cls, v: float, info: Any) -> float:
        min_hours = info.data.get("min_hours")
        if min_hours is not None and v < min_hours:
            raise ValueError("max_hours must be >= min_hours")
        return v


class GoldenStory(BaseModel):
    id: str
    scope: str
    kind: StoryKind
    hours: float = Field(gt=0)
    drivers: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError(f"id must match {_SLUG.pattern}")
        return v

    @field_validator("drivers")
    @classmethod
    def _known_drivers(cls, v: list[str]) -> list[str]:
        unknown = [d for d in v if d not in COMPLEXITY_DRIVERS]
        if unknown:
            raise ValueError(f"unknown drivers: {unknown}")
        if len(v) > MAX_DRIVERS_PER_STORY:
            raise ValueError(f"at most {MAX_DRIVERS_PER_STORY} drivers")
        return v


class GoldenAnchor(BaseModel):
    id: str
    ticket: str
    total_hours: float = Field(gt=0)
    active: bool = True
    stories: list[GoldenStory] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError(f"id must match {_SLUG.pattern}")
        return v

    @field_validator("stories")
    @classmethod
    def _unique_story_ids(cls, v: list[GoldenStory]) -> list[GoldenStory]:
        ids = [s.id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("story ids must be unique within an anchor")
        return v


class GoldenSet(BaseModel):
    version: int = 1
    bands: dict[str, Band]
    anchors: list[GoldenAnchor] = Field(default_factory=list)

    def resolve(self, anchor_ref: str) -> GoldenStory | None:
        """Find a story by "<anchor_id>#<story_id>", active or retired.

        Retired anchors stay resolvable on purpose: a historical analysis that
        cited one must still resolve in the calibration view.
        """
        if not anchor_ref or anchor_ref.count("#") != 1:
            return None
        anchor_id, story_id = anchor_ref.split("#")
        for anchor in self.anchors:
            if anchor.id != anchor_id:
                continue
            for story in anchor.stories:
                if story.id == story_id:
                    return story
        return None

    def active_pairs(self) -> list[tuple[GoldenAnchor, GoldenStory]]:
        """Every (anchor, story) an estimate may cite, in file order."""
        return [
            (anchor, story)
            for anchor in self.anchors
            if anchor.active
            for story in anchor.stories
        ]


def _default_bands() -> dict[str, Band]:
    return {
        name: Band(min_hours=lo, max_hours=hi)
        for name, (lo, hi) in DEFAULT_BANDS.items()
    }


def _bands_only() -> GoldenSet:
    return GoldenSet(bands=_default_bands(), anchors=[])


def load(prompts_dir: str) -> tuple[GoldenSet, list[Degradation]]:
    """Read and validate the anchor file, degrading rather than raising.

    A missing or broken file yields today's bands and no anchors, which is
    exactly today's behaviour — the feature is inert, never blocking.
    """
    path = os.path.join(prompts_dir, GOLDEN_FILENAME)
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return _bands_only(), [Degradation("file_missing", {"path": path})]
    except (OSError, yaml.YAMLError) as exc:
        return _bands_only(), [
            Degradation("file_unreadable", {"path": path, "error": str(exc)[:300]})
        ]

    if not isinstance(raw, dict):
        return _bands_only(), [Degradation("file_malformed", {"path": path})]

    degradations: list[Degradation] = []

    try:
        bands = {
            name: Band.model_validate(value)
            for name, value in (raw.get("bands") or {}).items()
        }
        missing = set(DEFAULT_BANDS) - set(bands)
        if missing:
            raise ValueError(f"missing bands: {sorted(missing)}")
    except (ValidationError, ValueError, AttributeError, TypeError) as exc:
        bands = _default_bands()
        degradations.append(Degradation("bands_invalid", {"error": str(exc)[:300]}))

    anchors: list[GoldenAnchor] = []
    seen: set[str] = set()
    for entry in raw.get("anchors") or []:
        entry_id = entry.get("id") if isinstance(entry, dict) else None
        try:
            anchor = GoldenAnchor.model_validate(entry)
            if anchor.id in seen:
                raise ValueError(f"duplicate anchor id: {anchor.id}")
        except (ValidationError, ValueError, TypeError) as exc:
            degradations.append(
                Degradation("anchor_invalid", {"id": entry_id, "error": str(exc)[:300]})
            )
            continue

        seen.add(anchor.id)
        story_sum = sum(s.hours for s in anchor.stories)
        if abs(anchor.total_hours - story_sum) / story_sum > _HOURS_MISMATCH_TOLERANCE:
            degradations.append(
                Degradation(
                    "anchor_hours_mismatch",
                    {
                        "id": anchor.id,
                        "total_hours": anchor.total_hours,
                        "story_sum": story_sum,
                    },
                )
            )
        anchors.append(anchor)

    version = raw.get("version", 1)
    return (
        GoldenSet(
            version=version if isinstance(version, int) else 1,
            bands=bands,
            anchors=anchors,
        ),
        degradations,
    )


DEFAULT_ANCHOR_LIMIT = 30

_BAND_ORDER = ("configuration", "small", "medium", "large")

_BAND_LABELS = {
    "configuration": "configuration / enabling a standard feature",
    "small": "small customization (new field, view tweak, constraint, visual marking, hard-block on confirm, simple wizard)",
    "medium": "medium customization (new model or copy mechanism + views + business logic)",
    "large": "large customization (cross-module workflow, status overview, complex computed logic)",
}

_PREAMBLE = """## Estimate calibration — binding

All figures are for a **mid-level Odoo developer working AI-assisted**, covering
**implementation + developer testing**. Exclude deployment, project management,
and customer communication. AI-assisted development is far faster than classic
agency quoting — never fall back to agency-style numbers. Estimate each story's
*incremental* effort: shared module scaffolding is priced once, never per story.

Sanity check before submitting: a typical 5–7-story custom module lands around
**15–30 h total**; if your sum is far above that, your per-story numbers are
inflated — revise them. Reference: a real 6-story module (order-bound BoM
copies, selective procurement release, per-line dropship route override,
availability status overview, placeholder-article hard block, margin popup)
took ≈ 15–25 h total."""

_HOW_TO_USE = """### How to use the anchors

- For each story, pick the **single closest anchor story** and set `anchor_ref`
  to its backticked id exactly as written above.
- Anchor the range on that story's hours. Adjust only for differences you can
  name in `assumptions`.
- Set `complexity_drivers` from this fixed list, at most 3, choosing what makes
  the work harder than its size suggests:
  {drivers}.
- If **no** anchor story is comparable, set `anchor_ref` to null and fall back
  to the bands above. That is a correct answer, not a failure — do not force a
  match.
- A ticket's stories almost always share one module. Sanity-check the total
  against the anchors' ticket totals above before submitting."""


def _fmt_hours(value: float) -> str:
    return f"{value:g}"


def render(
    golden: GoldenSet,
    limit: int = DEFAULT_ANCHOR_LIMIT,
    enabled: bool = True,
) -> tuple[str, list[Degradation]]:
    """Build the calibration block substituted into the estimating prompts.

    `enabled=False` renders bands only, which is today's behaviour. A set with
    no active anchors renders the same thing, so the feature stays inert until
    anchors are written.
    """
    degradations: list[Degradation] = []
    parts = [_PREAMBLE, "", "Bands — used only when no anchor below is comparable:", ""]
    for name in _BAND_ORDER:
        band = golden.bands.get(name)
        if band is None:
            continue
        parts.append(
            f"- {_BAND_LABELS[name]}: "
            f"**{_fmt_hours(band.min_hours)}–{_fmt_hours(band.max_hours)} h**"
        )

    pairs = golden.active_pairs() if enabled else []
    if not pairs:
        return "\n".join(parts), degradations

    if len(pairs) > limit:
        degradations.append(
            Degradation(
                "anchor_limit_exceeded",
                {"rendered": limit, "available": len(pairs)},
            )
        )
        pairs = pairs[:limit]

    parts += [
        "",
        "### Reference anchors — real closed tickets, actual booked hours",
        "",
        "Prefer these over the bands. They are what this work actually took.",
        "",
    ]
    current: str | None = None
    for anchor, story in pairs:
        if anchor.id != current:
            current = anchor.id
            parts.append(
                f"**{anchor.ticket}** — {_fmt_hours(anchor.total_hours)} h total"
            )
        drivers = ", ".join(story.drivers) if story.drivers else "none"
        parts.append(
            f"- `{anchor.id}#{story.id}` — {story.scope} — {story.kind} — "
            f"**{_fmt_hours(story.hours)} h** — drivers: {drivers}"
        )

    parts += ["", _HOW_TO_USE.format(drivers=", ".join(COMPLEXITY_DRIVERS))]
    return "\n".join(parts), degradations


def calibration_block(
    prompts_dir: str,
    limit: int = DEFAULT_ANCHOR_LIMIT,
    enabled: bool = True,
) -> tuple[str, list[Degradation]]:
    """Load the file and render its block. The entry point for every prompt site."""
    golden, degradations = load(prompts_dir)
    text, render_degradations = render(golden, limit=limit, enabled=enabled)
    return text, [*degradations, *render_degradations]


# Jaccard overlap at or above this counts as the same shape. A guess, tuned on
# real data later: it is a pure function with a truth table, so moving it is a
# one-line change.
_HIGH_OVERLAP = 0.6

Confidence = Literal["high", "medium", "low"]


def score(
    drivers: list[str],
    kind: str,
    anchor: GoldenStory | None,
) -> Confidence:
    """Derive estimate confidence from distance to the cited anchor story.

    This is what makes "confidence reflects anchor distance, not model
    self-assessment" true rather than aspirational: the model supplies facts
    (which anchor, which drivers), and code decides how close they are.

    The band is deliberately NOT part of the formula — band approximates hours,
    which is the value being predicted, so scoring on it would be circular.
    """
    if anchor is None or kind != anchor.kind:
        return "low"

    story_drivers = set(drivers)
    anchor_drivers = set(anchor.drivers)

    if not story_drivers and not anchor_drivers:
        # Identical "nothing unusual" profile. Jaccard is undefined here, so
        # this case is decided explicitly rather than by dividing by zero.
        return "high"

    union = story_drivers | anchor_drivers
    if not union:
        return "low"
    overlap = len(story_drivers & anchor_drivers) / len(union)

    if overlap >= _HIGH_OVERLAP:
        return "high"
    if overlap > 0:
        return "medium"
    return "low"


def apply_anchor(
    item: Any,
    golden: GoldenSet,
    *,
    score_confidence: bool,
) -> list[Degradation]:
    """Resolve an item's cited anchor and, for stories, derive its confidence.

    Mutates in place. `item` is a `StoryEstimate` (score_confidence=True) or a
    `TicketIssueItem` (False — an issue has no `kind` to score against).

    Driver sanitizing already happened in the Pydantic validator; this only
    handles the reference, which needs the loaded set the validator cannot see.
    """
    degradations: list[Degradation] = []
    anchor_story = None

    if item.anchor_ref:
        anchor_story = golden.resolve(item.anchor_ref)
        if anchor_story is None:
            degradations.append(
                Degradation("anchor_ref_unresolved", {"anchor_ref": item.anchor_ref})
            )
            item.anchor_ref = None

    if score_confidence:
        item.anchor_confidence = score(
            item.complexity_drivers, item.kind, anchor_story
        )

    return degradations
