# Licensing

SYNAPSE-24 is **dual-licensed**:

| Option | For whom | Terms |
|--------|----------|-------|
| **Open source** | Research, education, community and any use that can comply with the AGPL | [GNU AGPL v3.0 or later](LICENSE) |
| **Commercial** | Organizations that want to embed, ship or host SYNAPSE-24 without the AGPL copyleft obligations (e.g. closed-source products, firmware shipped in devices, hosted services without source disclosure) | Separate written agreement with the copyright holder |

Copyright (c) 2025 Alessio Brillo and contributors. SPDX identifier: `AGPL-3.0-or-later`.

## What the AGPL means in practice

- You may use, modify and redistribute the software, including commercially, **provided** that the whole covered work is offered under the AGPL with its complete corresponding source.
- Unlike the plain GPL, the obligation also applies when users interact with a modified version **over a network** (AGPL §13), not only when binaries are distributed.
- Distributing firmware images built from this repository in a device counts as conveying: recipients are entitled to the corresponding source and the right to modify it, subject to the AGPL terms.

This summary is informational. The text of [LICENSE](LICENSE) is the only authoritative statement of the open-source terms.

## Commercial licensing

Open a GitHub issue titled **"Commercial licensing inquiry"** in this repository, without confidential details; the maintainer will arrange a private channel. No commercial licence exists until an agreement is signed by the copyright holder.

## Third-party components

Dependencies keep their own licences and are **not** relicensed by either option above. See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md). A commercial licence covers only code owned by the copyright holder; obligations of third-party licences (attribution, MPL file-level terms, dataset terms) continue to apply to you.

## Device-side licence split

To keep the device firmware and host SDK adoptable (permissive, no copyleft)
while the host-side service stack stays protected by the AGPL:

| Component | Location | Licence | SPDX |
|-----------|----------|---------|------|
| Synapse-24 host package (`synapse24`), worker, local Data Plane backend | `src/synapse24/`, `worker/`, `backend/` | GNU AGPL v3.0 or later | `AGPL-3.0-or-later` |
| Synapse Band v1 firmware (ESP-IDF, C/C++) | `firmware/` | MIT | `MIT` |
| `synapse-device` host SDK (Python) | `sdk/python/` | Apache-2.0 | `Apache-2.0` |

The SDK deliberately never imports AGPL-licensed `synapse24` code, so it can
remain Apache-2.0 while `synapse24` depends on it. Firmware files carry the
`MIT` SPDX header; SDK files carry `Apache-2.0`; `synapse24`/worker/backend
files carry `AGPL-3.0-or-later`.

The root [LICENSE](LICENSE) (AGPL-3.0-or-later) governs the host package;
`firmware/LICENSE` and `sdk/python/LICENSE` govern the device-side deliverables.

## Contributions

Dual licensing only works if the maintainer may relicense every line. Contributors therefore grant that right explicitly; see [CONTRIBUTING.md](CONTRIBUTING.md#licensing-of-contributions).

## Earlier releases

Versions obtained before this licence change were published under the MIT License. Copies you already received under MIT remain yours under MIT; the AGPL / commercial terms apply to the versions that carry this notice and later ones.
