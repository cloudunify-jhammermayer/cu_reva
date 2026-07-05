"""Drift guards: contracts/ current; every callback path is published."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from reva.odoo_client import OdooCallbackClient
from reva.odoo_contracts import CONTRACTS, check

_ROOT = Path(__file__).resolve().parents[2]


def test_committed_contracts_are_current():
    problems = check(_ROOT / "contracts")
    assert not problems, (
        "contracts/ is stale; run `python -m reva.odoo_contracts generate` "
        f"and commit. Differences: {problems}"
    )


def test_every_callback_method_has_a_contract():
    published_paths = {contract.path for contract in CONTRACTS if contract.direction == "reva->odoo"}
    source = inspect.getsource(OdooCallbackClient)
    posted_paths = set(re.findall(r'self\._post\(\s*"([^"]+)"', source))
    unpublished = posted_paths - published_paths
    methods = [
        name for name, member in inspect.getmembers(OdooCallbackClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert not unpublished, (
        f"OdooCallbackClient posts to unpublished paths {unpublished}; add "
        "CONTRACTS entries in reva/odoo_contracts.py and regenerate contracts/."
    )
    assert methods
