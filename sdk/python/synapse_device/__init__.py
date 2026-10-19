# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""synapse-device — Host SDK for the Synapse Band v1.

Discover, configure, record, stream, export and analyze bio-sensing sessions
from a Synapse Band (ECG, PPG, IMU, GPS, temperature, power) without any
hardware dependency: a synthetic transport lets every command run offline.

The device layer is deliberately dependency-light and never imports the
AGPL-licensed ``synapse24`` package, so this SDK can stay Apache-2.0 while
``synapse24`` (AGPL-3.0) depends on *it*.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
