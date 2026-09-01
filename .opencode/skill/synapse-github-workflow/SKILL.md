---
name: synapse-github-workflow
description: Git branching, commit, PR, and CI/CD workflow for SYNAPSE-24. Trunk-based development, conventional commits, path-aware CI, baseline validation gates.
when_to_use: Before making any code change, committing, pushing, or creating PRs in SYNAPSE-24.
user-invocable: true
---

# SYNAPSE GitHub Workflow

Single source of truth for all Git and GitHub operations in SYNAPSE-24. Adapted from Ergodix workflow for Python/uv stack.

---

## 1. Branching Convention

### Strategy: Trunk-Based Development
- **Only permanent branch**: `main`
- **All work**: Short-lived branches (hours to 2 days max)
- **Never commit directly to `main`** — every change via branch + PR

### Branch Naming
```
<type>/<description>
```

| Type | Use Case |
|------|----------|
| `feat` | New feature (ingestion, quality metric, hardware support) |
| `fix` | Bug fix |
| `refactor` | Code restructuring, no behavior change |
| `docs` | Documentation only |
| `style` | Formatting, no functional change |
| `perf` | Performance improvement |
| `test` | Adding/correcting tests |
| `build` | Build system, dependencies, tooling |
| `ci` | CI/CD configuration |
| `chore` | Maintenance, config |

**Rules**:
- Description: lowercase kebab-case, max 72 chars total
- Examples: `feat/lsl-xdf-writer`, `fix/mitbih-rpeak-tolerance`, `refactor/tier-thresholds`

---

## 2. Full Lifecycle

```
1. BRANCH    — git checkout -b <type>/<description>
2. CODE      — Implement following synapse-architecture-guardian
3. COMMIT    — git add + conventional commit (see §3)
4. PREFLIGHT — uv run preflight (local quality gate)
5. PUSH      — git push -u origin <branch>
6. CI        — Wait for GitHub Actions (path-filtered)
7. PR        — gh pr create (see §6)
8. REVIEW    — Address feedback
9. MERGE     — Squash-merge into main
10. CLEANUP  — Delete feature branch locally and remote
```

---

## 3. Commit Standard (Conventional Commits 1.0.0)

### Structure
```
<type>(<scope>): <description>

<body>

<footer>
```

### Scope Derivation
From changed file paths under `src/synapse24/`:
- `ingestion` → `ingestion`
- `signal_quality` → `quality`
- `utils` → `utils`
- `hardware` → `hardware`
- `acquisition` → `acquisition`
- Multiple → broader category or omit

### Description Rules
- Imperative present tense: "add", not "added"
- Max 50 chars, no trailing period, lowercase first letter
- Completes: "If applied, this commit will <description>"

### Body Rules
- Blank line after description
- Wrap at 72 chars
- Explain WHY, not WHAT
- Reference ADRs, issues: `Refs: ADR-0001`, `Closes: #123`

### Footer Rules
- `BREAKING CHANGE: <description>` if backward compatibility broken
- `Refs: ADR-NNNN` for architecture decisions
- `Closes: #<issue>` for issue tracking

### Example
```
feat(quality): add tier-aware thresholds with literature citations

Tier 0 (continuous) now uses relaxed thresholds (PPG SQI >= 0.5)
while Tier 1 (high-density sleep) enforces strict thresholds
(PPG SQI >= 0.7, EEG flatness <= 0.3). Thresholds cite
WESAD (Schmidt 2018), EmotiBit (Chen 2024), MIT-BIH gold standard.

Refs: ADR-0003
Closes: #42
```

---

## 4. Preflight — Local Quality Gate

**Run before every push**:
```bash
# Full preflight (recommended)
uv run preflight

# Quick: skip tests
uv run preflight quick

# Diff audit only
uv run preflight diff
```

### Preflight Checks
| Check | Command | Blocking |
|-------|---------|----------|
| Format | `uv run ruff format --check .` | ✅ |
| Lint | `uv run ruff check .` | ✅ |
| Typecheck | `uv run mypy --strict src/synapse24` | ✅ (new code) |
| Unit Tests | `uv run pytest tests/unit -x --tb=short` | ✅ |
| Integration Tests | `uv run pytest tests/integration -x` | ✅ |
| Baseline Validation | `uv run python scripts/validate_baseline.py --dataset both` | ✅ |
| Security | `uv run bandit -r src/` | ⚠️ Warning |
| Energy Budget | `uv run python scripts/energy_budget_check.py` | ⚠️ Warning |

**Do not push if preflight fails**. Fix locally, re-run.

---

## 5. Push & Continuous Integration

### GitHub Actions Workflow (`.github/workflows/ci.yml`)

```yaml
# Path-aware job filtering (saves ~70% CI minutes)
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      ingestion: ${{ steps.filter.outputs.ingestion }}
      quality: ${{ steps.filter.outputs.quality }}
      utils: ${{ steps.filter.outputs.utils }}
      hardware: ${{ steps.filter.outputs.hardware }}
      tests: ${{ steps.filter.outputs.tests }}
    steps:
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            ingestion:
              - 'src/synapse24/ingestion/**'
              - 'scripts/ingest_datasets.py'
            quality:
              - 'src/synapse24/signal_quality/**'
            utils:
              - 'src/synapse24/utils/**'
            hardware:
              - 'src/synapse24/hardware/**'
            tests:
              - 'tests/**'
              - 'scripts/validate_baseline.py'

  lint-typecheck:
    needs: changes
    if: always()  # Always run for formatting/typecheck
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run mypy --strict src/synapse24

  test-unit:
    needs: [changes, lint-typecheck]
    if: needs.changes.outputs.tests == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run pytest tests/unit -x --cov=src --cov-fail-under=85

  test-integration:
    needs: [changes, lint-typecheck]
    if: needs.changes.outputs.ingestion == 'true' || needs.changes.outputs.quality == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - uses: actions/cache@v4
        with:
          path: data/wesad
          key: wesad-${{ hashFiles('pyproject.toml') }}
      - uses: actions/cache@v4
        with:
          path: data/mitbih
          key: mitbih-${{ hashFiles('pyproject.toml') }}
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run pytest tests/integration -x

  baseline-validation:
    needs: [changes, test-integration]
    if: needs.changes.outputs.ingestion == 'true' || needs.changes.outputs.quality == 'true'
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - uses: actions/cache@v4
        with:
          path: data/wesad
          key: wesad-${{ hashFiles('pyproject.toml') }}
      - uses: actions/cache@v4
        with:
          path: data/mitbih
          key: mitbih-${{ hashFiles('pyproject.toml') }}
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run python scripts/validate_baseline.py --dataset both --data-dir data --output-dir data/processed
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: baseline-validation
          path: data/processed/*_validation.json
          retention-days: 30

  test-e2e:
    needs: [changes, test-unit]
    if: needs.changes.outputs.tests == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run pytest tests/e2e -x

  security-audit:
    needs: lint-typecheck
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv python install 3.11
      - run: uv sync --dev --frozen
      - run: uv run bandit -r src/ -f json -o bandit-report.json || true
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: security-audit
          path: bandit-report.json
          retention-days: 30
```

### Local Quality Gates (Before Push)
- [ ] `git status` — only intended files staged
- [ ] `uv run ruff format --check .` — passes
- [ ] `uv run ruff check .` — passes
- [ ] `uv run mypy --strict src/synapse24` — passes (new code)
- [ ] `uv run pytest tests/unit -x` — passes
- [ ] No `print()`, `TODO`, `FIXME`, `console.log` in diff
- [ ] Architecture principles satisfied (see guardian skill)
- [ ] No hardcoded secrets, MAC addresses, API keys

---

## 6. Pull Requests

### Creating PR
```bash
gh pr create --title "<conventional-commit-title>" --body "$(cat <<'EOF'
## Summary
- <what changed and why — 2-3 bullets>

## Related
- Refs: ADR-NNNN (if applicable)
- Closes: #<issue-number> (if applicable)

## Checklist
- [ ] All relevant CI checks pass (path-filtered)
- [ ] Architecture principles satisfied
- [ ] Signal quality thresholds documented with citations
- [ ] LSL timestamp monotonicity verified
- [ ] Energy budget impact assessed (µJ/sample)
- [ ] Tests added for new metrics / ingestion paths
- [ ] Documentation updated (README, docstrings)
- [ ] Branch rebased on latest main
EOF
)"
```

### PR Title
Must follow Conventional Commits (same as commit). Used as squash-merge message.

### PR Requirements
- ✅ All triggered CI checks pass
- ✅ At least 1 reviewer approval
- ✅ No merge conflicts with `main` (rebase if needed)
- ✅ Squash-merge (clean linear history)

---

## 7. Non-Negotiable Safety Rules

- ❌ Never commit directly to `main`
- ❌ Never force-push to `main` or shared branches
- ❌ Never amend pushed commits — add new commit instead
- ❌ Never commit credentials, `.env`, `data/`, `__pycache__/`, `.venv/`
- ❌ All output in English: code, comments, commits, docs, variables
- ✅ Feature flags for incomplete features (no long-lived branches)
- ✅ One concern per branch

---

## 8. Release Process

### Versioning: SemVer via Git Tags
```bash
# After merge to main, tag release
git tag -a v0.2.0 -m "Release v0.2.0: LSL/XDF backbone, tier-aware thresholds"
git push origin v0.2.0
```

### GitHub Actions Release Job
```yaml
release:
  needs: [test-unit, test-integration, baseline-validation, test-e2e]
  if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0 }
    - uses: astral-sh/setup-uv@v3
    - run: uv python install 3.11
    - run: uv sync --dev --frozen
    - run: uv build
    - uses: pypa/gh-action-pypi-publish@release/v1
      with:
        password: ${{ secrets.PYPI_API_TOKEN }}
```

---

## References
- Ergodix github-workflow skill (pattern source)
- Conventional Commits: https://www.conventionalcommits.org/
- Architecture.md, Roadmap.md (governing)