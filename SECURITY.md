# Security Policy

## Supported versions

SYNAPSE-24 is pre-1.0 (`0.x`). Security fixes are applied to the latest commit on `master` only.

## Reporting a vulnerability

**Do not open a public issue containing vulnerability details.**

1. Preferred: use GitHub's **Security → Report a vulnerability** on this repository (private advisory).
2. If that option is not available, open a public issue titled **"Security contact request"** with *no technical details*; the maintainer will move the conversation to a private channel.

Please include affected component (firmware / `src/synapse24` / scripts), version or commit, reproduction steps and impact.

## What to expect

This is a small-maintainer project; there is no contractual SLA. We aim to acknowledge reports within 7 days and to agree a disclosure date with the reporter, typically within 90 days of the report.

## Scope

In scope: this repository's source code, firmware and build/CI configuration. Particularly relevant given the data handled (see [PRIVACY.md](PRIVACY.md)): BLE/Wi-Fi provisioning and OTA paths in `firmware/`, handling of recorded XDF/LSL data, and secrets in the repository.

Out of scope: vulnerabilities in third-party dependencies that are not exploitable through SYNAPSE-24 (report those upstream), and physical attacks on prototype hardware.
