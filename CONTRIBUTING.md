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

## Developer Certificate of Origin (DCO)

To ensure clear intellectual property provenance under our dual-licensing model (AGPLv3 / Commercial), all contributions to SYNAPSE-24 are subject to the **Developer Certificate of Origin (DCO)**.

By contributing to this project, you certify that:
1. The contribution was created in whole or in part by you and you have the right to submit it under the project's license; or
2. The contribution is based upon previous work that, to the best of your knowledge, is covered under an appropriate open-source license and you have the right under that license to submit that work; and
3. You understand and agree that this project and the contribution are public and that a record of the contribution (including all personal information you submit with it) is maintained indefinitely and may be redistributed consistent with the project's licensing.

To certify your DCO sign-off, add a `Signed-off-by` line to every git commit message:

```
git commit -m "feat(acquisition): add multi-pod sync marker handler

Signed-off-by: Your Name <your.email@example.com>"
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

[optional footer with Signed-off-by]
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
