"""Phase 0 real-data closure: MIT-BIH all-QRS reference + WESAD poison guard.

Architecture.md Tier 1 strict gates on the MIT-BIH clinical gold standard
(Moody & Mark 2001): R-peak Se >= 0.996, PPV >= 0.996, RMSSD MAE < 5 ms.
Roadmap.md Phase 0 exit gate: reproduce published baselines on real data
before any Phase 1 hardware procurement.

Root causes locked by these tests (verified on cached data/mitbih):
- R-peak reference restricted to normal beats only punished detection of
  real premature ventricular complexes (record 119: 444 V beats) and
  inflated RMSSD MAE by orders of magnitude (gaps where beats were
  excluded). The reference must be ALL QRS complexes.
- A failed UCI mirror returned HTTP 200 with an HTML error page saved as
  data/wesad/WESAD.zip (poison cache). The downloader must validate zip
  integrity and remove poison files instead of leaving them behind.
- Stale pre-spec XDF artifacts (8-byte length prefix before the magic)
  fail pyxdf with "Invalid XDF file". The writer must emit magic first.

Markers: ``baseline`` (real-data, excluded from quick/unit runs; skipped
in CI when datasets are not cached).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

DATA_DIR = Path(__file__).parent.parent / "data"
MITBIH_DIR = DATA_DIR / "mitbih"

requires_mitbih_100 = pytest.mark.skipif(
    not (MITBIH_DIR / "100.dat").exists(), reason="MIT-BIH record 100 not cached"
)
requires_mitbih_119 = pytest.mark.skipif(
    not (MITBIH_DIR / "119.dat").exists(), reason="MIT-BIH record 119 not cached"
)


@pytest.mark.baseline
class TestMitbihAllQrsReference:
    """Reference peaks must cover every QRS complex, not just normal beats."""

    @requires_mitbih_119
    def test_reference_includes_ventricular_beats_119(self) -> None:
        """Record 119 holds 1543 N + 444 V beats: reference must count 1987."""
        from synapse24.ingestion.mitbih import load_mitbih_record

        _, reference_peaks, metadata = load_mitbih_record("119", MITBIH_DIR)
        assert metadata["n_reference_beats"] == len(reference_peaks)
        assert len(reference_peaks) == 1987

    @requires_mitbih_100
    def test_record100_tier1_gates_on_real_data(self) -> None:
        """Clean record 100 must clear Tier 1 strict gates (Se/PPV/MAE)."""
        from synapse24.ingestion.mitbih import load_mitbih_record
        from synapse24.signal_quality import (
            detect_r_peaks_neurokit,
            r_peak_detection_quality,
            rmssd_mae,
        )

        ecg_signal, reference_peaks, metadata = load_mitbih_record("100", MITBIH_DIR)
        fs = int(metadata["fs"])
        detected = detect_r_peaks_neurokit(ecg_signal, fs)
        sensitivity, ppv = r_peak_detection_quality(detected, reference_peaks, fs)
        mae = rmssd_mae(detected, reference_peaks, fs)
        assert sensitivity >= 0.996
        assert ppv >= 0.996
        assert mae < 5.0

    @requires_mitbih_119
    def test_record119_pvc_heavy_tier1_gates_on_real_data(self) -> None:
        """PVC-heavy record 119 must clear Tier 1 gates with all-QRS reference."""
        from synapse24.ingestion.mitbih import load_mitbih_record
        from synapse24.signal_quality import (
            detect_r_peaks_neurokit,
            r_peak_detection_quality,
            rmssd_mae,
        )

        ecg_signal, reference_peaks, metadata = load_mitbih_record("119", MITBIH_DIR)
        fs = int(metadata["fs"])
        detected = detect_r_peaks_neurokit(ecg_signal, fs)
        sensitivity, ppv = r_peak_detection_quality(detected, reference_peaks, fs)
        mae = rmssd_mae(detected, reference_peaks, fs)
        assert sensitivity >= 0.996
        assert ppv >= 0.996
        assert mae < 5.0


@pytest.mark.baseline
class TestMitbihXdfValidity:
    """Regenerated XDF artifacts must be pyxdf-readable (zero-drop gate)."""

    @requires_mitbih_100
    def test_process_record_writes_valid_xdf(self, tmp_path: Path) -> None:
        """process_mitbih_record output must pass validate_xdf."""
        from synapse24.ingestion.mitbih import process_mitbih_record
        from synapse24.utils import validate_xdf

        result = process_mitbih_record("100", MITBIH_DIR, tmp_path)
        summary = validate_xdf(Path(result["xdf_path"]))
        assert summary["validation"]["all_streams_valid"]
        assert summary["n_streams"] == 3


class TestMitbihCorruptionQuarantine:
    """Zero-filled/corrupt records (local 201 case) must fail loudly."""

    def test_flatline_signal_rejected(self) -> None:
        """A constant-valued signal is acquisition corruption, not bradycardia."""
        from synapse24.ingestion import mitbih as mitbih_mod

        flat = np.full(3600, -5.12)
        with pytest.raises(ValueError, match="flatline"):
            mitbih_mod._reject_corrupt_record("999", flat, np.array([100, 200, 300]))

    def test_empty_annotation_set_rejected(self) -> None:
        """Zero reference beats means a corrupt .atr, never a clean record."""
        from synapse24.ingestion import mitbih as mitbih_mod

        rng = np.random.default_rng(42)
        with pytest.raises(ValueError, match="no reference beats"):
            mitbih_mod._reject_corrupt_record(
                "999", rng.standard_normal(3600), np.array([], dtype=np.int64)
            )

    def test_healthy_record_passes_guard(self) -> None:
        """A physiological signal with beats must not trip the guard."""
        from synapse24.ingestion import mitbih as mitbih_mod

        rng = np.random.default_rng(42)
        sig = rng.standard_normal(3600)
        mitbih_mod._reject_corrupt_record("999", sig, np.array([100, 200, 300]))


class TestXdfWriterFormat:
    """Regression guard for the stale-format root cause (magic must lead)."""

    def test_write_xdf_starts_with_magic(self, tmp_path: Path) -> None:
        """Writer output must start with b'XDF:' or pyxdf rejects the file."""
        from synapse24.utils import StreamConfig, write_xdf

        out = tmp_path / "magic.xdf"
        write_xdf(
            out,
            [
                {
                    "data": np.zeros((64, 1)),
                    "timestamps": np.arange(64, dtype=np.float64) / 64.0,
                    "info": StreamConfig(
                        name="SYNAPSE_TEST",
                        stream_type="ECG",
                        channel_count=1,
                        sampling_rate=64.0,
                    ),
                }
            ],
        )
        assert out.read_bytes()[:4] == b"XDF:"


class TestWesadPoisonGuard:
    """The downloader must never leave an HTML error page behind as .zip."""

    def test_html_body_rejected_and_cleaned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 200 + zip content-type + HTML body -> None, no poison file."""
        import synapse24.ingestion.wesad as wesad_mod

        poison = b"<!DOCTYPE html><html><body>Access Denied</body></html>"

        class _FakeResponse:
            status_code = 200
            headers = {"content-type": "application/zip", "content-length": str(len(poison))}

            def iter_content(self, chunk_size: int = 8192):
                yield poison

        def _fake_get(*args: object, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

        monkeypatch.setattr(wesad_mod.requests, "get", _fake_get)
        result = wesad_mod.download_wesad(tmp_path)
        assert result is None
        assert not (tmp_path / "WESAD.zip").exists()
        assert not (tmp_path / "WESAD").exists()
