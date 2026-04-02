# AGENTS.md - AWS Deadline Cloud job attachments

This document provides guidance for AI agents working with this codebase.

## Project Overview

AWS Deadline Cloud job attachments is a Python library for transferring files between workstations and [AWS Deadline Cloud](https://docs.aws.amazon.com/deadline-cloud/latest/userguide/what-is-deadline-cloud.html) using Amazon S3 buckets as content-addressed storage.

**Package name:** `deadline-job-attachments`
**Python versions:** 3.8 - 3.14
**Platforms:** Linux, Windows, macOS

## Repository Structure

```
src/deadline/
└── job_attachments/  # File transfer to/from S3
    ├── api/          # Public job attachments API
    ├── caches/       # Hash and S3 check caches
    └── asset_manifests/  # Manifest handling
```

## Key Commands

Always use `hatch` for running builds, tests, formatting, and linting.
If running just a few tests, always add `--numprocesses=1` to the `hatch run test`
command so that it starts quicker.

```bash
# Development
hatch build              # Build wheel/sdist
hatch run test           # Run unit tests
hatch run test --numprocesses=1 -k "selected_test_name" # Run just a few tests
hatch run all:test       # Test all Python versions
hatch run integ:test     # Integration tests (requires AWS)
hatch run lint           # Check formatting
hatch run fmt            # Auto-format code
hatch shell              # Enter dev environment
```

## Code Conventions

### Module Naming
- **Private modules:** `_module.py` - internal implementation
- **Public modules:** `module.py` - part of public API
- Symbols not starting with `_` define the public contract

### Import Style (Public Modules)
```python
import os as _os  # Private import
from typing import Dict as _Dict  # Private import

class PublicClass:  # Public
    pass

class _PrivateClass:  # Private
    pass
```

### Commit Messages
Use [conventional commits](https://www.conventionalcommits.org/):
- `feat:` - New features
- `fix:` - Bug fixes
- `docs:` - Documentation
- `test:` - Tests only
- `refactor:` - Code refactoring
- `perf:` - Performance improvements
- `feat!:` or `fix!:` - Breaking changes (Also include `BREAKING CHANGES:` section in message body)

### Before Committing / Raising a PR

After completing any code changes:
1. Always run `hatch run fmt` and `hatch run lint` to verify the changes are clean.
2. If also committing, run the full checklist: fmt → lint → test → build → `git commit -s`.

```bash
hatch run fmt            # Auto-format code
hatch run lint           # Linting and type checking
hatch run test           # Unit tests (must pass with ≥80% coverage)
hatch build              # Build wheel/sdist
git commit -s            # Always sign commits with -s
```

## Testing

- **Unit tests:** `test/unit/` - Run with `hatch run test`
- **Integration tests:** `test/integ/` - Requires AWS resources
- **Docker tests:** `scripts/run_sudo_tests.sh` - For permission tests

Tests use pytest with unittest.mock. Coverage target: 80%.

## Public API Contracts

Breaking changes require conventional commit syntax (`feat!:`, `fix!:`).

**Python API contracts:**
- All non-underscore-prefixed functions/classes in public modules
- Function signatures (adding required params is breaking)
- Default argument values

## Dependencies

Minimize new dependencies. This library is used from embedded Python within
applications where dependency conflicts are possible. When adding:
- Evaluate if functionality can be implemented locally
- Check library quality (maintenance, downloads, stars)
- Ensure license compatibility (Apache-2.0)
- Document in THIRD_PARTY_LICENSES

## Key Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Project config, dependencies |
| `CONTRIBUTING.md` | Contribution guidelines |
| `DEVELOPMENT.md` | Development setup and workflows |
| `CHANGELOG_GUIDELINES.md` | Changelog formatting rules |
| `scripts/README.md` | API change detection scripts |
