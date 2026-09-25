"""Dataset integrity tests — verify SHA-256, poison-cache detection, and structure."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

DATA_ROOT = Path(__file__).parent.parent / "data"
WESAD_DIR = DATA_ROOT / "wesad" / "WESAD"
MITBIH_DIR = DATA_ROOT / "mitbih"
SLEEP_EDF_DIR = DATA_ROOT / "sleep_edf"

# Markers for conditional test execution
requires_wesad = pytest.mark.skipif(not WESAD_DIR.exists(), reason="WESAD not cached")
requires_mitbih = pytest.mark.skipif(not MITBIH_DIR.exists(), reason="MIT-BIH not cached")
requires_sleep_edf = pytest.mark.skipif(not SLEEP_EDF_DIR.exists(), reason="Sleep-EDF not cached")


class TestWesadIntegrity:
    """WESAD dataset integrity checks."""

    @requires_wesad
    def test_wesad_directory_structure(self):
        """WESAD extraction must have all 15 subject directories (S2-S17, no S12)."""
        subjects = sorted(
            [d.name for d in WESAD_DIR.iterdir() if d.is_dir() and d.name.startswith("S")]
        )
        expected = [f"S{i}" for i in range(2, 18) if i != 12]
        assert subjects == expected, f"Missing subjects: {set(expected) - set(subjects)}"

    @requires_wesad
    def test_wesad_subject_pickle_files(self):
        """Each subject must have exactly one .pkl file with expected keys."""
        for subj_dir in WESAD_DIR.iterdir():
            if not subj_dir.is_dir() or not subj_dir.name.startswith("S"):
                continue
            pkl_files = list(subj_dir.glob("*.pkl"))
            assert len(pkl_files) == 1, f"{subj_dir.name}: expected 1 .pkl, found {len(pkl_files)}"
            import pickle

            with open(pkl_files[0], "rb") as f:
                data = pickle.load(f, encoding="latin1")
            # Required top-level keys
            assert "signal" in data, f"{subj_dir.name}: missing 'signal' key"
            assert "label" in data, f"{subj_dir.name}: missing 'label' key"
            assert "subject" in data, f"{subj_dir.name}: missing 'subject' key"
            # Signal structure
            assert "chest" in data["signal"], f"{subj_dir.name}: missing chest signals"
            assert "wrist" in data["signal"], f"{subj_dir.name}: missing wrist signals"
            chest = data["signal"]["chest"]
            for key in ["ECG", "EDA", "EMG", "Resp", "Temp", "ACC"]:
                assert key in chest, f"{subj_dir.name}: chest missing {key}"
            wrist = data["signal"]["wrist"]
            for key in ["BVP", "EDA", "TEMP", "ACC"]:
                assert key in wrist, f"{subj_dir.name}: wrist missing {key}"

    @requires_wesad
    def test_wesad_no_poison_zip(self):
        """Ensure no poison HTML files masquerading as zip exist."""
        zip_files = list((WESAD_DIR.parent).glob("*.zip"))
        for zip_file in zip_files:
            assert zipfile.is_zipfile(zip_file), f"Poison zip detected: {zip_file}"
            # Additional: verify it's not an HTML file
            with open(zip_file, "rb") as f:
                header = f.read(512).lower()
            assert not header.startswith(b"<!doctype html"), f"HTML poison in {zip_file}"
            assert b"<html" not in header, f"HTML poison in {zip_file}"


class TestMitbihIntegrity:
    """MIT-BIH dataset integrity checks."""

    @requires_mitbih
    def test_mitbih_closure_records_exist(self):
        """Phase 0 closure records (100, 119) must be present with .dat and .atr."""
        for rec in ["100", "119"]:
            dat = MITBIH_DIR / f"{rec}.dat"
            atr = MITBIH_DIR / f"{rec}.atr"
            assert dat.exists(), f"Missing {dat}"
            assert atr.exists(), f"Missing {atr}"
            assert dat.stat().st_size > 1000, f"{dat} appears empty/corrupt"
            assert atr.stat().st_size > 100, f"{atr} appears empty/corrupt"

    @requires_mitbih
    def test_mitbih_record_100_clean_sinus(self):
        """Record 100 must be clean sinus with ~2265 normal beats."""
        import numpy as np
        import wfdb

        record = wfdb.rdrecord(str(MITBIH_DIR / "100"))
        annotation = wfdb.rdann(str(MITBIH_DIR / "100"), "atr")
        symbols = np.array([str(s) for s in annotation.symbol])
        # Count QRS symbols
        qrs_symbols = {
            "N",
            "L",
            "R",
            "B",
            "A",
            "a",
            "J",
            "S",
            "V",
            "r",
            "F",
            "e",
            "j",
            "n",
            "E",
            "/",
            "f",
            "Q",
        }
        qrs_count = sum(1 for s in symbols if s in qrs_symbols)
        # Record 100: ~2265 beats in 30 min at 360 Hz
        assert 2200 <= qrs_count <= 2350, f"Record 100 unexpected QRS count: {qrs_count}"
        # Signal sanity
        ecg = record.p_signal[:, 0] if record.p_signal.ndim > 1 else record.p_signal.flatten()
        assert float(np.ptp(ecg)) > 0.05, "Record 100 flatline (corrupt .dat)"

    @requires_mitbih
    def test_mitbih_record_119_pvc_heavy(self):
        """Record 119 must have 444 V beats (PVC-heavy)."""
        import numpy as np
        import wfdb

        record = wfdb.rdrecord(str(MITBIH_DIR / "119"))
        annotation = wfdb.rdann(str(MITBIH_DIR / "119"), "atr")
        symbols = np.array([str(s) for s in annotation.symbol])
        v_count = sum(1 for s in symbols if s == "V")
        assert v_count >= 400, f"Record 119 missing V beats: found {v_count}, expected ~444"
        n_count = sum(1 for s in symbols if s == "N")
        assert n_count >= 1500, f"Record 119 missing N beats: found {n_count}"
        total_qrs = sum(
            1
            for s in symbols
            if s
            in {
                "N",
                "L",
                "R",
                "B",
                "A",
                "a",
                "J",
                "S",
                "V",
                "r",
                "F",
                "e",
                "j",
                "n",
                "E",
                "/",
                "f",
                "Q",
            }
        )
        assert total_qrs == 1987, f"Record 119 total QRS should be 1987, got {total_qrs}"

    @requires_mitbih
    def test_mitbih_no_zero_filled_records(self):
        """No record should be zero-filled (flatline corruption)."""
        import numpy as np
        import wfdb

        for dat_file in MITBIH_DIR.glob("*.dat"):
            rec_id = dat_file.stem
            try:
                record = wfdb.rdrecord(str(MITBIH_DIR / rec_id))
                ecg = (
                    record.p_signal[:, 0] if record.p_signal.ndim > 1 else record.p_signal.flatten()
                )
                ptp = float(np.ptp(ecg))
                assert ptp > 0.05, f"Record {rec_id}: flatline ptp={ptp:.4f} mV (corrupt .dat)"
            except Exception as e:
                pytest.fail(f"Record {rec_id} failed to load: {e}")


def _load_download_datasets_module():
    """Load download_datasets module from scripts directory."""
    import importlib.util
    import sys

    script_path = Path(__file__).parent.parent / "scripts" / "download_datasets.py"
    spec = importlib.util.spec_from_file_location("scripts.download_datasets", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclasses can find it
    sys.modules["scripts.download_datasets"] = module
    spec.loader.exec_module(module)
    return module


class TestDatasetDownloadScript:
    """Test the download_datasets.py script functionality."""

    @classmethod
    def setup_class(cls):
        cls.download_module = _load_download_datasets_module()

    def test_compute_sha256(self, tmp_path: Path):
        """SHA-256 computation works correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello world")
        hash_val = self.download_module.compute_sha256(test_file)
        assert hash_val == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    def test_verify_sha256(self, tmp_path: Path):
        """SHA-256 verification works correctly."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"hello world")
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert self.download_module.verify_sha256(test_file, expected)
        assert not self.download_module.verify_sha256(test_file, "wrong_hash")

    def test_is_poison_zip_detects_html(self, tmp_path: Path):
        """Poison zip detection catches HTML error pages."""
        # Valid zip
        import zipfile

        valid_zip = tmp_path / "valid.zip"
        with zipfile.ZipFile(valid_zip, "w") as zf:
            zf.writestr("test.txt", "content")
        assert not self.download_module.is_poison_zip(valid_zip)

        # HTML poison
        html_zip = tmp_path / "poison.zip"
        html_zip.write_bytes(b"<!DOCTYPE html><html><body>Access Denied</body></html>")
        assert self.download_module.is_poison_zip(html_zip)

        # XML poison
        xml_zip = tmp_path / "poison2.zip"
        xml_zip.write_bytes(b"<?xml version='1.0'?><error>Rate Limited</error>")
        assert self.download_module.is_poison_zip(xml_zip)

        # 403 page
        forbidden = tmp_path / "poison3.zip"
        forbidden.write_bytes(b"<html><body>403 Forbidden</body></html>")
        assert self.download_module.is_poison_zip(forbidden)


class TestDownloadConfig:
    """Test DownloadConfig defaults."""

    @classmethod
    def setup_class(cls):
        cls.download_module = _load_download_datasets_module()

    def test_default_config(self):
        config = self.download_module.DownloadConfig()
        assert config.max_retries == 3
        assert config.base_delay_s == 10.0
        assert config.verify_sha256 is True
        assert config.verify_zip_integrity is True
        assert config.cleanup_on_failure is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
