# 12 — Configuration

## Configuration Layers

Configuration is resolved in three layers, from lowest to highest priority:

1. **Global defaults** (`config/default.yml` in the monorepo)
2. **Per-repo config** (`.claude-review.yml` in the repo root)
3. **Manual trigger overrides** (`/review` or `/deep-review` comment)

## Global Defaults (config/default.yml)

```yaml
# config/default.yml — Global defaults for all repositories

review:
  enabled: true
  default_mode: diff                # diff | deep
  debounce_seconds: 600             # 10 minutes
  max_diff_lines: 1000              # decline PRs above this
  max_findings: 15                  # cap findings per review

triggers:
  on_opened: true
  on_synchronize: true
  on_reopened: true
  on_ready_for_review: true
  skip_draft: true
  skip_forks: true
  manual_trigger: true              # allow /review and /deep-review comments

model:
  default: claude-sonnet-4-6
  deep: claude-opus-4-7
  max_tokens: 8192

skip_paths:
  - "*.lock"
  - "*.min.js"
  - "*.min.css"
  - "*.map"
  - "vendor/**"
  - "node_modules/**"
  - "dist/**"
  - "build/**"
  - "*.generated.*"
  - "__pycache__/**"
  - "*.pyc"
  - "static/lib/**"
  - "*.po"                          # translation files
  - "*.pot"

deep_review_paths:
  - "**/auth/**"
  - "**/security/**"
  - "**/payment*/**"
  - "**/migration*/**"
  - "**/permissions/**"

severity:
  post_inline_comments: true
  min_severity_for_inline: info     # info | minor | major | critical
  min_confidence_for_inline: 0.0    # post all, adjust after testing
  check_run_failure_threshold: major # major or critical findings → failure

github_output:
  create_check_run: true
  create_pr_review: true
  review_event: COMMENT             # COMMENT | REQUEST_CHANGES

notifications:
  on_critical: true
  on_failure: true
  on_major: false                   # enable after initial testing

odoo:
  enabled: false                    # overridden per repo
  version: "19"
```

## Per-Repo Config (.claude-review.yml)

Each repo can override global defaults by placing a `.claude-review.yml` in the root. The worker fetches this file from GitHub at review time (using the head SHA, so it's always the version in the PR).

```yaml
# .claude-review.yml — Per-repo overrides

# Enable/disable review for this repo
enabled: true

# Framework hint — enables framework-specific review rules
framework: odoo                    # odoo | django | fastapi | generic
odoo_version: "19"                 # only relevant when framework=odoo

# Review mode override
default_mode: diff                 # diff | deep

# Override max diff lines for this repo
max_diff_lines: 800                # stricter than global default

# Additional paths to skip (merged with global skip_paths)
skip_paths:
  - "static/src/lib/**"
  - "i18n/**"
  - "*.csv"                        # demo data

# Paths that always trigger deep review (merged with global)
deep_review_paths:
  - "models/account_*.py"
  - "models/hr_*.py"
  - "security/**"
  - "data/ir_rule*.xml"

# Custom instructions for ARIA (appended to system prompt)
# Use this for repo-specific conventions that CLAUDE.md doesn't cover
custom_instructions: |
  This module handles multi-company accounting.
  Pay special attention to company_id filtering.
  All monetary fields must use currency_id for rounding.

# Severity overrides
severity:
  min_severity_for_inline: minor   # suppress info-level inline comments
  min_confidence_for_inline: 0.5   # only post if confidence >= 0.5
```

## CLAUDE.md

The `CLAUDE.md` file in a repo root provides project-specific context to ARIA. Unlike `.claude-review.yml` (which configures behavior), `CLAUDE.md` provides knowledge.

Example:

```markdown
# CLAUDE.md

## Project Overview
This is an Odoo 19 module for B2B e-commerce integration. It connects
Odoo sales orders with the customer's procurement system via EDI.

## Architecture
- `models/edi_order.py` — main EDI order processing
- `models/edi_partner.py` — partner mapping and validation
- `controllers/edi_webhook.py` — incoming EDI webhook handler
- `wizards/edi_import.py` — manual import wizard

## Conventions
- All EDI-related models are prefixed with `edi.`
- Use `edi.mixin` for shared EDI fields
- External API calls go through `services/edi_client.py`
- Never call external APIs inside compute methods
- All monetary values are in EUR unless explicitly converted

## Known Issues
- The partner matching logic in `edi_partner.py` is a known weak point.
  We plan to rewrite it using fuzzy matching. Don't flag it for now.
- `edi_order._process_line_items()` is intentionally complex due to
  legacy EDI format variations. Refactoring is tracked in JIRA-1234.

## Testing
- Run tests with: `python odoo-bin -d test --test-tags /edi`
- All controller endpoints must have integration tests
```

The worker reads `CLAUDE.md` from the repo and appends it to the system prompt under a "Repository-Specific Instructions" section.

## Config Loading Logic

```python
# worker/config_loader.py

import yaml
from pathlib import Path

DEFAULT_CONFIG_PATH = "/app/config/default.yml"

class ConfigLoader:
    def __init__(self):
        with open(DEFAULT_CONFIG_PATH) as f:
            self.defaults = yaml.safe_load(f)

    def load_repo_config(self, github_client, token, owner, repo, sha) -> dict:
        """Load and merge repo config with global defaults."""
        # Try to fetch .claude-review.yml from repo
        raw = github_client.get_file_content(token, owner, repo, ".claude-review.yml", sha)

        if raw:
            repo_config = yaml.safe_load(raw)
        else:
            repo_config = {}

        # Merge: repo overrides global
        merged = deep_merge(self.defaults, repo_config)
        return merged

    def load_claude_md(self, github_client, token, owner, repo, sha) -> str | None:
        """Load CLAUDE.md from repo root."""
        return github_client.get_file_content(token, owner, repo, "CLAUDE.md", sha)


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Lists are concatenated (skip_paths), scalars are overridden."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            # For paths, merge and deduplicate
            result[key] = list(set(result[key] + value))
        else:
            result[key] = value
    return result
```

## Skip Path Matching

Skip paths use glob patterns. The worker checks each changed file against the combined skip list:

```python
import fnmatch

def should_skip_file(file_path: str, skip_patterns: list[str]) -> bool:
    for pattern in skip_patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
    return False

def filter_diff(diff: str, changed_files: list[dict], skip_patterns: list[str]) -> tuple[str, list]:
    """Remove skipped files from diff and file list."""
    filtered_files = [f for f in changed_files if not should_skip_file(f["filename"], skip_patterns)]
    # Also filter the diff string to remove hunks for skipped files
    filtered_diff = remove_skipped_hunks(diff, skip_patterns)
    return filtered_diff, filtered_files
```

## Deep Review Path Matching

If any changed file matches a deep review path pattern, the review mode is upgraded from `diff` to `deep` (unless overridden by manual trigger):

```python
def should_upgrade_to_deep(changed_files: list[str], deep_patterns: list[str]) -> bool:
    for file_path in changed_files:
        for pattern in deep_patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return True
    return False
```

## Config Caching

The worker caches the parsed `.claude-review.yml` in `repositories.config_cache` (JSONB column) to avoid fetching it on every review. Cache is refreshed if `config_cached_at` is older than 1 hour or if the file's content hash changed.
