# Security scanning — setup & operation

What this covers and the one principle behind it: **everything here is $0 on your
GitHub Team plan and needs no API keys or extra subscriptions.** We deliberately
use free CLI tools in GitHub Actions instead of GitHub's paid **Code Security**
(CodeQL) and **Secret Protection** add-ons (≈ $30 / $19 per active committer per
month). Coverage is equivalent for a Python + Go repo this size.

Most of it is already wired up in code (committed). **One thing needs you to flip
a switch in the GitHub UI: Dependabot alerts + security updates.** That's the only
manual step.

---

## Quick status

| Tool | What it catches | Wired where | You must enable? |
|---|---|---|---|
| **Dependabot — alerts + security updates** | Known-vulnerable dependencies; opens auto-fix PRs | GitHub UI toggle | ✅ **Yes — see step 1** |
| **Dependabot — version updates** | Routine dependency bumps (grouped, weekly) | `.github/dependabot.yml` (committed) | No (starts once the file is on the default branch) |
| **gitleaks** | Secrets committed to the repo | `security` job in `.github/workflows/ci.yml` | No (runs in CI) |
| **Semgrep** | SAST — risky code patterns (Python + Go) | `security` job in `.github/workflows/ci.yml` | No (runs in CI) |

> Not used on purpose: **GitHub code scanning (CodeQL)** and **GitHub secret
> scanning** — both require paid Team add-ons. Leave them off. gitleaks + Semgrep
> are the free stand-ins.

---

## 1. Enable Dependabot alerts + security updates  *(the only manual step)*

**Why:** on Team this is free and is the highest-value, lowest-noise control —
GitHub watches your dependencies against its advisory database and (with security
updates on) opens a PR that bumps a vulnerable package to a fixed version. Nothing
to run; GitHub does it.

**Where:** your repo → **Settings** → **Code security and analysis** (some orgs
label it **Advanced Security** / **Security and analysis**).

**How:**
1. **Dependabot alerts** → **Enable**.
2. **Dependabot security updates** → **Enable** (this is what opens the auto-fix PRs).
3. (Org-wide alternative: Org **Settings → Code security and analysis → Enable all**, and "Automatically enable for new repositories".)

That's it — no token, no cost on Team.

---

## 2. Dependabot version updates  *(already configured)*

`.github/dependabot.yml` (committed) keeps dependencies current with **grouped,
weekly** PRs — one PR per area (worker / api / scheduler deps, the Go TUI, and the
GitHub Actions pins) so you get a handful of PRs a week, not dozens.

**Where:** it activates automatically once the file is on the default branch.
**How to tune:** change `interval: weekly`, or add `open-pull-requests-limit`, or
`ignore:` specific packages in that file.

---

## 3. gitleaks — secret scanning  *(already configured, advisory)*

**Why:** stops credentials (keys, tokens, PEMs) from being committed. Relevant
here — a placeholder key was once committed to this repo's history.

**Where:** the `security` job in `.github/workflows/ci.yml`, on every push/PR.

**How it's set up:**
- It scans the **working tree** (`gitleaks dir`), **not** full git history — so the
  one historical placeholder commit doesn't permanently fail the build. (To audit
  history once, run locally: `docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks:latest git /repo`.)
- It runs **advisory** for now (`continue-on-error: true`) so it can't break CI
  before you've seen its first output.
- **If it flags test placeholders** (e.g. `ghs_test`, the `.env.example` sample),
  add a `.gitleaks.toml` allowlist at the repo root rather than weakening the scan:
  ```toml
  [allowlist]
  paths = ['''\.env\.example$''', '''.*/tests/.*''']
  ```

**Make it blocking** (after reviewing the first run): delete the
`continue-on-error: true` line under the gitleaks step.

**Optional — catch secrets before commit, locally:**
```bash
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks: [{ id: gitleaks }]
```

---

## 4. Semgrep — SAST  *(already configured, advisory)*

**Why:** flags risky code patterns (injection, unsafe subprocess, auth gaps) across
**both** Python and Go with one tool — no per-language linters to manage, and no
account/token (we pin public rulesets, not `--config auto`).

**Where:** the `security` job in `.github/workflows/ci.yml`.

**How it's set up:**
- Rulesets: `p/python`, `p/golang`, `p/security-audit` (public Semgrep registry, no login).
- Runs **advisory** (`continue-on-error: true`) for now.
- **If it flags a false positive:** add a `# nosemgrep: <rule-id>` comment on the
  line, or a `.semgrepignore` file for paths to skip.

**Make it blocking** (after reviewing the first run): delete the
`continue-on-error: true` line under the semgrep step.

---

## First-run checklist

1. ✅ Enable Dependabot alerts + security updates (step 1) — the only UI action.
2. Merge the branch carrying `.github/dependabot.yml` + the CI `security` job.
3. Watch the first **security scan (advisory)** job run; review gitleaks + Semgrep output.
4. Add allowlist entries for any genuine false positives (steps 3–4).
5. When the scans come back clean, **remove the two `continue-on-error: true` lines**
   so they become real gates. Do **not** enable GitHub's paid CodeQL / secret-scanning.

---

## A note for whoever runs this

I couldn't execute gitleaks or Semgrep in the dev sandbox (they fetch rules /
images at runtime), so they ship **advisory** on purpose. The first CI run is the
real test — expect to add a couple of allowlist entries for test fixtures, then
flip them to blocking. That's the intended graduation path, mirroring how mypy and
golangci-lint were introduced.
