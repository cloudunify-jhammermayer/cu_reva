"""Pure rules behind the docs site's Internal modules page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from reva.product_catalog import (
    BranchModule,
    ModuleMeta,
    build_branch,
    classify_issues,
    is_version_branch,
    merge_repo,
    module_dirs,
    parse_product_yml,
    select_version_branches,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


# --- branches -----------------------------------------------------------------

def test_is_version_branch():
    assert is_version_branch("19.0")
    assert is_version_branch("20.0")
    assert not is_version_branch("19.1")
    assert not is_version_branch("saas-19.4")
    assert not is_version_branch("main")
    assert not is_version_branch("v19.0")


def test_select_branches_sorts_numerically():
    read, ignored = select_version_branches(["9.0", "main", "20.0", "18.0", "19.0", "17.0"])
    assert read == ["20.0", "19.0", "18.0"]
    assert ignored == ["17.0", "9.0"]


def test_select_branches_fewer_than_three():
    assert select_version_branches(["main", "19.0"]) == (["19.0"], [])
    assert select_version_branches(["main"]) == ([], [])


# --- product.yml --------------------------------------------------------------

def test_parse_product_yml_full_entry():
    entries, warnings = parse_product_yml(
        "modules:\n"
        "  cu_helpdesk_sla:\n"
        "    owner: Joseph H.\n"
        "    price: 1200\n"
        "    tldr: SLA timers\n"
        "    features:\n      - A\n      - B\n"
        "  cu_helpdesk_kb:\n"
        "    price: free\n    status: planned\n    eta: 2027-Q1\n"
        "  cu_helpdesk_chat:\n"
        "    status: discontinued\n    note: Replaced by core\n"
    )
    assert warnings == []
    sla = entries["cu_helpdesk_sla"]
    assert (sla.owner, sla.price, sla.tldr, sla.features, sla.status) == (
        "Joseph H.", 1200, "SLA timers", ["A", "B"], "available",
    )
    kb = entries["cu_helpdesk_kb"]
    assert (kb.price, kb.status, kb.eta) == ("free", "planned", "2027-Q1")
    assert entries["cu_helpdesk_chat"].note == "Replaced by core"


def test_parse_product_yml_tolerates_missing_and_malformed():
    assert parse_product_yml(None) == ({}, [])
    assert parse_product_yml("") == ({}, [])
    entries, warnings = parse_product_yml("modules: [\n")
    assert entries == {} and warnings and "not valid YAML" in warnings[0]
    entries, warnings = parse_product_yml("- just\n- a list\n")
    assert entries == {} and "modules" in warnings[0]
    entries, warnings = parse_product_yml("modules:\n  cu_x: just a string\n")
    assert entries == {} and "cu_x" in warnings[0]


def test_parse_product_yml_impossible_date_degrades_to_warning():
    entries, warnings = parse_product_yml("modules:\n  cu_a:\n    eta: 2026-13-45\n")
    assert entries == {} and len(warnings) == 1 and "not valid YAML" in warnings[0]


def test_parse_product_yml_coerces_and_warns():
    entries, warnings = parse_product_yml(
        "modules:\n"
        "  19.0:\n    owner: Num Key\n"            # numeric key -> "19.0"
        "  cu_a:\n    eta: 2026-12-01\n"           # YAML date -> "2026-12-01"
        "  cu_b:\n    price: '1200'\n"             # string price -> warning, None
        "  cu_c:\n    features: not a list\n"      # -> warning, []
        "  cu_d:\n    status: in_development\n"    # unknown -> warning, available
        "  cu_e:\n    price: 99.5\n"               # float is fine
        "  cu_f:\n"                                # empty entry is fine
    )
    assert entries["19.0"].owner == "Num Key"
    assert entries["cu_a"].eta == "2026-12-01"
    assert entries["cu_b"].price is None
    assert entries["cu_c"].features == []
    assert entries["cu_d"].status == "available"
    assert entries["cu_e"].price == 99.5
    assert entries["cu_f"] == ModuleMeta()
    joined = "\n".join(warnings)
    assert "cu_b.price" in joined and "cu_c.features" in joined and "cu_d.status" in joined
    assert len(warnings) == 3


# --- modules per branch -------------------------------------------------------

def test_module_dirs_top_level_manifests_only():
    assert module_dirs([
        "cu_a/__manifest__.py", "cu_b/__manifest__.py", "cu_b/models/x.py",
        "nested/cu_c/__manifest__.py", "README.md", "cu_a/README.md",
    ]) == ["cu_a", "cu_b"]


def test_build_branch_merges_manifest_yml_and_readme():
    yml, _ = parse_product_yml(
        "modules:\n  cu_a:\n    owner: J\n    status: discontinued\n    note: gone\n"
        "  cu_planned:\n    status: planned\n    eta: Q1\n"
        "  cu_ghost:\n    owner: X\n"
    )
    rows, warnings = build_branch(
        "19.0",
        {"cu_a": "{'name': 'A', 'summary': 'Sum', 'version': '19.0.1.0.0'}", "cu_broken": "import os"},
        {"cu_a/README.md"},
        yml,
    )
    assert set(rows) == {"cu_a", "cu_broken", "cu_planned"}
    a = rows["cu_a"]
    assert (a.name, a.summary) == ("A", "Sum")
    assert a.version == {
        "status": "discontinued", "version": "19.0.1.0.0", "eta": None, "note": "gone",
        "manifest_error": None, "readme_path": "cu_a/README.md",
    }
    broken = rows["cu_broken"]
    assert broken.meta is None
    assert broken.version["manifest_error"] and broken.version["version"] is None
    assert broken.version["readme_path"] is None
    assert rows["cu_planned"].version == {
        "status": "planned", "version": None, "eta": "Q1", "note": None,
        "manifest_error": None, "readme_path": None,
    }
    assert warnings == ["product.yml on 19.0: `cu_ghost` has no module directory; ignored"]


def _bm(module, branch_meta=None, name=None, summary=None, version="19.0.1.0.0"):
    return BranchModule(
        module=module, name=name, summary=summary, meta=branch_meta,
        version={"status": branch_meta.status if branch_meta else "available", "version": version,
                 "eta": None, "note": None, "manifest_error": None, "readme_path": None},
    )


def test_merge_repo_highest_branch_wins_and_reports_drift():
    hi = ModuleMeta(owner="Joseph", price=1200, tldr="New words", features=["x"])
    lo = ModuleMeta(owner="Markus", price=1200, tldr="Old words", features=["y"])
    modules, warnings = merge_repo([
        ("19.0", {"cu_a": _bm("cu_a", hi, name="A"), "cu_only19": _bm("cu_only19", None, summary="S")}),
        ("18.0", {"cu_a": _bm("cu_a", lo, name="A old", version="18.0.1.0.0")}),
    ])
    assert [m["module"] for m in modules] == ["cu_a", "cu_only19"]
    a = modules[0]
    assert (a["name"], a["owner"], a["price"], a["tldr"], a["features"]) == (
        "A", "Joseph", 1200, "New words", ["x"],
    )
    assert a["has_yml_entry"] is True
    assert set(a["versions"]) == {"19.0", "18.0"}
    assert a["versions"]["18.0"]["version"] == "18.0.1.0.0"
    only = modules[1]
    assert only["has_yml_entry"] is False
    assert only["tldr"] == "S"          # manifest summary fallback
    assert only["owner"] is None and only["price"] is None
    assert "18.0" not in only["versions"]
    assert warnings == ["`cu_a`: owner differs between 19.0 and 18.0"]


def test_merge_repo_yml_tldr_beats_manifest_summary_same_branch():
    meta = ModuleMeta(tldr="From yml")
    modules, _ = merge_repo([("19.0", {"cu_a": _bm("cu_a", meta, summary="From manifest")})])
    assert modules[0]["tldr"] == "From yml"


# --- issues -------------------------------------------------------------------

def _issue(number, state="open", **kw):
    base = {
        "number": number, "title": f"[feature] 6{number:03d} - thing {number}",
        "html_url": f"https://github.com/acme/x/issues/{number}", "state": state,
        "created_at": f"2026-09-{number:02d}T00:00:00Z", "assignees": [], "assignee": None,
        "milestone": None, "closed_at": None, "state_reason": None,
    }
    base.update(kw)
    return base


def test_classify_issues_states_and_order():
    issues = [
        _issue(1),                                                   # planned
        _issue(2, assignees=[{"login": "joseph"}]),                 # in_progress (assignee)
        _issue(3),                                                   # in_progress (linked PR)
        _issue(4, state="closed", state_reason="not_planned",
               closed_at="2026-10-01T00:00:00Z"),                    # wont_do
        _issue(5, state="closed", state_reason="completed",
               closed_at="2026-10-01T00:00:00Z"),                    # dropped
        _issue(6, pull_request={"url": "x"}),                        # PR masquerading, dropped
        _issue(7, milestone={"title": "19.0.2"}),                    # planned with eta
    ]
    prs = [{"number": 50, "title": "Fix #3 timers", "body": None},
           {"number": 51, "title": "Unrelated", "body": "see acme/other#1"}]
    out = classify_issues(issues, prs, NOW)
    assert [(i["number"], i["state"]) for i in out] == [
        (3, "in_progress"), (2, "in_progress"), (7, "planned"), (1, "planned"), (4, "wont_do"),
    ]
    by = {i["number"]: i for i in out}
    assert by[2]["assignee"] == "joseph" and by[2]["pr_number"] is None
    assert by[3]["pr_number"] == 50
    assert by[7]["milestone"] == "19.0.2"
    assert by[4]["closed_at"] == "2026-10-01T00:00:00Z"
    assert by[1]["url"].endswith("/issues/1")


def test_classify_issues_wont_do_window():
    old = (NOW - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (NOW - timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%SZ")
    issues = [
        _issue(1, state="closed", state_reason="not_planned", closed_at=old),
        _issue(2, state="closed", state_reason="not_planned", closed_at=recent),
        _issue(3, state="closed", state_reason="not_planned", closed_at=None),
    ]
    assert [i["number"] for i in classify_issues(issues, [], NOW)] == [2]


def test_classify_issues_capped_at_50():
    issues = [_issue(n) for n in range(1, 61)]
    assert len(classify_issues(issues, [], NOW)) == 50


# --- subscriptions -------------------------------------------------------------

def test_parse_product_yml_subscription_needs_per():
    entries, warnings = parse_product_yml(
        "modules:\n"
        "  cu_a:\n    price: 1200\n    subscription: 90\n    per: month\n"   # both prices
        "  cu_b:\n    subscription: 900\n    per: year\n"                   # subscription only
        "  cu_c:\n    subscription: 50\n"                                   # no per -> ignored
        "  cu_d:\n    subscription: 50\n    per: week\n"                    # bad per -> ignored
        "  cu_e:\n    subscription: cheap\n    per: month\n"               # bad amount -> ignored
    )
    assert (entries["cu_a"].price, entries["cu_a"].subscription, entries["cu_a"].per) == (
        1200, 90, "month",
    )
    assert (entries["cu_b"].price, entries["cu_b"].subscription, entries["cu_b"].per) == (
        None, 900, "year",
    )
    for name in ("cu_c", "cu_d", "cu_e"):
        assert entries[name].subscription is None and entries[name].per is None
    joined = "\n".join(warnings)
    assert "cu_c.per" in joined and "cu_d.per" in joined and "cu_e.subscription" in joined
    assert len(warnings) == 3


def test_merge_repo_carries_subscription_and_reports_drift():
    hi = ModuleMeta(owner="J", price=1200, subscription=90, per="month")
    lo = ModuleMeta(owner="J", price=1200, subscription=80, per="month")
    modules, warnings = merge_repo([
        ("19.0", {"cu_a": _bm("cu_a", hi)}),
        ("18.0", {"cu_a": _bm("cu_a", lo)}),
    ])
    assert (modules[0]["subscription"], modules[0]["per"]) == (90, "month")
    assert warnings == ["`cu_a`: subscription differs between 19.0 and 18.0"]
