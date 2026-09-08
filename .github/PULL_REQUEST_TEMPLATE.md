# Pull Request Template

## Description

<!-- Provide a clear description of what this PR does -->

## Type of Change

<!-- Check all that apply -->

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactor (no functional changes)
- [ ] Performance improvement
- [ ] CI/CD changes
- [ ] Test additions/improvements

## Related Issues

<!-- Link to related issues using "Fixes #123" or "Relates to #123" -->

Fixes #

## Architecture Alignment

<!-- How does this change align with Architecture.md and Roadmap.md? Reference specific sections. -->

- Architecture.md section(s):
- Roadmap.md phase/section:

## Changes Made

<!-- List the key changes in this PR -->

- 
- 
- 

## Testing

<!-- Describe how you tested your changes -->

### Test Coverage

- [ ] Unit tests added/updated for new functionality
- [ ] Integration tests added/updated
- [ ] Baseline validation tests pass (if applicable)

### Test Commands Run

```bash
# All tests with coverage
uv run pytest --cov=src --cov-fail-under=80

# Specific test file
uv run pytest tests/test_<module>.py -v
```

### Test Results

<!-- Paste relevant test output or coverage summary -->

```
Coverage: XX.XX%
Tests: XXX passed, X skipped
```

## Quality Gates

<!-- Verify all quality gates pass locally before requesting review -->

- [ ] **Tests pass**: `uv run pytest --cov=src --cov-fail-under=80`
- [ ] **Coverage ≥80%**: Line + branch coverage
- [ ] **Lint clean**: `uv run ruff check .`
- [ ] **Format clean**: `uv run ruff format --check .`
- [ ] **Type check clean**: `uv run mypy --package synapse24`
- [ ] **Baseline validation**: `uv run python scripts/validate_baseline.py --dataset both` (if ingestion/signal_quality changes)

## Documentation

- [ ] README.md updated (if user-facing changes)
- [ ] Architecture.md updated (if architectural changes)
- [ ] Roadmap.md updated (if roadmap changes)
- [ ] Docstrings added/updated for public APIs
- [ ] Type hints added for new functions
- [ ] CHANGELOG.md updated (if applicable)

## Security

- [ ] No hardcoded secrets, API keys, or credentials
- [ ] No sensitive data in logs or error messages
- [ ] Input validation for new user-facing functions
- [ ] SQL injection prevention (if database changes)

## Breaking Changes

<!-- If this is a breaking change, describe the impact and migration path -->

- [ ] No breaking changes
- [ ] Breaking changes documented below:

**Impact:**
**Migration Path:**

## Screenshots/Artifacts

<!-- If applicable, add screenshots, XDF validation output, or other artifacts -->

## Checklist

- [ ] My code follows the project's coding standards
- [ ] I have performed a self-review of my code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] New and existing unit tests pass locally with my changes
- [ ] Any dependent changes have been merged and published

## Additional Notes

<!-- Any additional information, configuration, or data that might be relevant to this PR -->