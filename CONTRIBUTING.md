# Contributing to SYNAPSE-24

Thank you for your interest in contributing to SYNAPSE-24! This project implements a 24/7 multimodal bio-sensing wearable platform with tiered acquisition, edge AI, and LSL/XDF synchronization.

## Code of Conduct

By participating, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting Started

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Git

### Development Setup

```bash
# Clone the repository
git clone https://github.com/AlessioBrillo/SYNAPSE-24.git
cd SYNAPSE-24

# Install dependencies with uv (fast, reproducible)
uv sync --dev

# Install pre-commit hooks
uv run pre-commit install

# Verify setup
uv run pytest --cov=src --cov-fail-under=80 -q
uv run ruff check .
uv run mypy --package synapse24
```

## Branch Strategy

| Branch Type | Naming Convention | Purpose |
|-------------|-------------------|---------|
| Feature | `feature/<short-description>` | New functionality |
| Bug Fix | `fix/<short-description>` | Bug fixes |
| Refactor | `refactor/<short-description>` | Code improvements |
| Documentation | `docs/<short-description>` | Documentation updates |
| Chore | `chore/<short-description>` | Maintenance tasks |

**Main branch**: `main` (protected, requires PR review and CI pass)

## Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types

- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `docs`: Documentation only changes
- `test`: Adding or modifying tests
- `chore`: Maintenance tasks (deps, build, CI)
- `perf`: Performance improvement
- `ci`: CI/CD changes
- `security`: Security-related changes

### Examples

```
feat(ingestion): add WESAD surrogate dataset support
fix(signal_quality): correct PPG SQI calculation for low perfusion
refactor(edge_ai): simplify quantization config API
docs(readme): update quickstart with uv commands
test(acquisition): add tier transition integration tests
```

## Pull Request Process

### Before Opening a PR

1. **Run the full test suite locally:**
   ```bash
   uv run pytest --cov=src --cov-fail-under=80
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy --package synapse24
   ```

2. **Ensure your changes include tests** for new functionality (target ≥80% coverage)

3. **Update documentation** if you change public APIs or add features

4. **Follow the architecture** defined in [Architecture.md](Architecture.md) and [Roadmap.md](Roadmap.md)

### PR Checklist

- [ ] Tests pass (including baseline validation where applicable)
- [ ] Coverage ≥80% (line + branch)
- [ ] Lint clean (`ruff check .`)
- [ ] Format clean (`ruff format --check .`)
- [ ] Type check clean (`mypy --package synapse24`)
- [ ] Documentation updated (README, docstrings, type hints)
- [ ] Conventional commit messages
- [ ] No hardcoded secrets or credentials
- [ ] Security review for new API endpoints or auth changes

### Review Process

1. CI must pass (lint, typecheck, tests, baseline validation)
2. At least one maintainer review required
3. Address all review comments
4. Squash commits if requested
5. Merge via "Squash and merge" to keep history clean

## Issue Triage

Issues are labeled with:

| Label | Meaning |
|-------|---------|
| `bug` | Something isn't working |
| `enhancement` | New feature or improvement |
| `documentation` | Documentation needs update |
| `good first issue` | Good for newcomers |
| `help wanted` | Extra attention needed |
| `priority: high` | Blocking or critical |
| `priority: medium` | Important but not blocking |
| `priority: low` | Nice to have |
| `area: ingestion` | Dataset ingestion pipelines |
| `area: signal_quality` | Signal quality metrics |
| `area: edge_ai` | Edge AI / TinyML |
| `area: acquisition` | Tier state machine, hardware |
| `area: lsl_xdf` | Synchronization, XDF I/O |

## Architecture Alignment

All changes must align with the governing documents:

- **Architecture.md**: Decoupled sensor/hub, tiered acquisition (T0/T1/T2), edge triage, energy budgets
- **Roadmap.md**: Phase-based execution, public dataset validation, LSL sync, Edge Impulse/TFLM pipeline

If a change affects architecture, update both documents and include the ADR (Architecture Decision Record) in the PR.

## Testing Standards

### Test Types

1. **Unit Tests** (`tests/test_*.py`): Individual functions, pure logic
2. **Integration Tests**: Pipeline tests with mocked external dependencies
3. **Baseline Validation**: Tests against published benchmarks (WESAD, MIT-BIH, Sleep-EDF)

### Coverage Requirements

- Minimum 80% line + branch coverage
- Critical paths (signal quality, tier transitions, XDF I/O): aim for 95%+
- New modules: add tests before or alongside implementation

### Running Tests

```bash
# All tests with coverage
uv run pytest --cov=src --cov-fail-under=80

# Specific module
uv run pytest tests/test_signal_quality.py -v

# With coverage report
uv run pytest --cov=src --cov-report=html

# Skip slow/integration tests
uv run pytest -m "not slow and not integration"
```

## Security

- **Never commit secrets** (API keys, tokens, passwords)
- Use environment variables for configuration
- Report security vulnerabilities privately to the maintainers
- See [Security Policy](SECURITY.md) if it exists

## Coding Standards

### Python Style

- Follow [PEP 8](https://pep8.org/) via Ruff
- Type hints required for all public functions (strict mypy)
- Google-style docstrings for public APIs
- Maximum line length: 100 characters
- Use `pathlib.Path` for filesystem operations
- Prefer `numpy.typing.NDArray` for array type hints

### Imports

```python
# Standard library
import json
from pathlib import Path
from typing import Any

# Third party
import numpy as np
import numpy.typing as npt

# Local
from synapse24.signal_quality import QualityThresholds
```

### Error Handling

- Use specific exceptions, not bare `except:`
- Log errors with context, don't swallow silently
- Validate inputs early with Pydantic or manual checks

## Documentation

- Update `README.md` for user-facing changes
- Update `Architecture.md` / `Roadmap.md` for architectural changes
- Docstrings for all public classes/functions
- Type hints serve as inline documentation

## Release Process

Releases are automated via GitHub Actions on tag push:

```bash
# Create release (maintainers only)
git tag -a v0.2.0 -m "Release v0.2.0: Add fNIRS ingestion pipeline"
git push origin v0.2.0
```

Versioning follows [Semantic Versioning](https://semver.org/).

## Questions?

Open a [Discussion](https://github.com/AlessioBrillo/SYNAPSE-24/discussions) or check existing issues.

---

*SYNAPSE-24 — Maximizing physiological data quality, quantity, and diversity for pattern recognition.*