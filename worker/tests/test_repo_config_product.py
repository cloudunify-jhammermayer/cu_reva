"""`product: true` in .claude-review.yml marks a sellable-addons repo and widens
the review scope, because product repos keep their addons at the repo root."""

from __future__ import annotations

from reva.types import RepoConfig


def test_product_implies_review_all_paths():
    cfg = RepoConfig.model_validate({"product": True})
    assert cfg.product is True
    assert cfg.review_all_paths is True


def test_product_false_leaves_review_all_paths_alone():
    assert RepoConfig.model_validate({"product": False}).review_all_paths is False
    assert RepoConfig.model_validate({}).review_all_paths is False
    assert RepoConfig.model_validate({"review_all_paths": True}).product is False
