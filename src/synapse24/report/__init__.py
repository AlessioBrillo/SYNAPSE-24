"""SessionReport v1 — representation of a single recorded session for Averyn.

Public API:
- build_session_report(record, job_results=None, decimation_factor=10) -> dict
- report_quality_note(report, note)
"""

from __future__ import annotations

from .report import REPORT_VERSION, build_session_report, report_quality_note

__all__ = ["REPORT_VERSION", "build_session_report", "report_quality_note"]
