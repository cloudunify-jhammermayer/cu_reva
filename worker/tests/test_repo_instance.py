"""The Odoo instance a repo declares in .claude-review.yml."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog
from sqlalchemy import select

from reva.db.engine import Database, create_engine_from_url
from reva.db.models import Base, OdooInstance, OpsEvent, Repository
from reva.errors import PermanentError, TransientError
from worker.repo_instance import UnknownRepoInstance, UnreadableRepoConfig, declared_instance_id

_log = structlog.get_logger()


@pytest.fixture()
def db():
    engine = create_engine_from_url("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Database(engine)


def _ctx(db, config=None):
    github = MagicMock()
    github.get_installation_token.return_value = "tok"
    github.get_file_content.return_value = config
    return SimpleNamespace(db=db, github=github)


def _seed_repo(db):
    with db.session() as s:
        s.add(Repository(id=3, github_repository_id=1003, owner="acme", name="widgets",
                         full_name="acme/widgets", installation_id=99, enabled=True,
                         default_branch="develop"))


def _seed_instance(db, name="customer", active=True):
    with db.session() as s:
        s.add(OdooInstance(name=name, key_hash=f"h-{name}", key_prefix=f"rk_{name}",
                           active=active))
        s.flush()
        return s.execute(select(OdooInstance.id).where(OdooInstance.name == name)).scalar_one()


def _events(db):
    with db.session() as s:
        return [e.event for e in s.execute(select(OpsEvent)).scalars().all()]


def _declared(ctx):
    return declared_instance_id(ctx, "acme/widgets", 99, _log)


def test_unregistered_repo_declares_nothing(db):
    ctx = _ctx(db, "odoo_instance: customer\n")
    assert _declared(ctx) is None
    ctx.github.get_file_content.assert_not_called()


def test_no_config_file_declares_nothing(db):
    _seed_repo(db)
    assert _declared(_ctx(db, None)) is None


def test_config_without_the_key_declares_nothing(db):
    _seed_repo(db)
    assert _declared(_ctx(db, "odoo: true\n")) is None


def test_declared_and_active_instance_is_returned(db):
    _seed_repo(db)
    instance_id = _seed_instance(db)
    ctx = _ctx(db, "odoo_instance: customer\n")
    assert _declared(ctx) == instance_id
    # The config is read at the repo's default branch.
    assert ctx.github.get_file_content.call_args.args[-1] == "develop"


def test_declared_but_unknown_instance_raises_with_the_name(db):
    _seed_repo(db)
    with pytest.raises(UnknownRepoInstance) as exc:
        _declared(_ctx(db, "odoo_instance: customer\n"))
    assert exc.value.args[0] == "customer"


def test_declared_but_inactive_instance_raises(db):
    _seed_repo(db)
    _seed_instance(db, active=False)
    with pytest.raises(UnknownRepoInstance):
        _declared(_ctx(db, "odoo_instance: customer\n"))


def test_malformed_config_is_unreadable_with_an_ops_event(db):
    _seed_repo(db)
    with pytest.raises(UnreadableRepoConfig):
        _declared(_ctx(db, "odoo_instance: [unclosed\n"))
    assert "repo_instance_config_failed" in _events(db)


def test_permanent_github_error_is_unreadable_with_an_ops_event(db):
    _seed_repo(db)
    ctx = _ctx(db)
    ctx.github.get_file_content.side_effect = PermanentError("403")
    with pytest.raises(UnreadableRepoConfig):
        _declared(ctx)
    assert "repo_instance_config_failed" in _events(db)


def test_non_mapping_top_level_is_unreadable(db):
    _seed_repo(db)
    with pytest.raises(UnreadableRepoConfig):
        _declared(_ctx(db, "- odoo_instance\n- customer\n"))
    assert "repo_instance_config_failed" in _events(db)


@pytest.mark.parametrize("value", ["123", '""'])
def test_odoo_instance_that_is_not_a_non_empty_string_is_unreadable(db, value):
    _seed_repo(db)
    with pytest.raises(UnreadableRepoConfig):
        _declared(_ctx(db, f"odoo_instance: {value}\n"))
    assert "repo_instance_config_failed" in _events(db)


def test_invalid_unrelated_key_does_not_hide_the_declared_instance(db):
    _seed_repo(db)
    instance_id = _seed_instance(db)
    ctx = _ctx(db, "odoo_instance: customer\nblock_on_severity: high\n")
    assert _declared(ctx) == instance_id


def test_null_odoo_instance_declares_nothing(db):
    _seed_repo(db)
    assert _declared(_ctx(db, "odoo_instance: null\n")) is None


def test_empty_yaml_declares_nothing(db):
    _seed_repo(db)
    assert _declared(_ctx(db, "# nothing\n")) is None


def test_transient_github_error_propagates(db):
    _seed_repo(db)
    ctx = _ctx(db)
    ctx.github.get_file_content.side_effect = TransientError("503")
    with pytest.raises(TransientError):
        _declared(ctx)
