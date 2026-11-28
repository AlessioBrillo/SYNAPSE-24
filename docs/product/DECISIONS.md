# Product decision record — open risks for the first commercial image

This file records **decisions and open risks**, not shipped features. It is the
input to the first product design freeze; none of the items below is
implemented by MVP-2.10. The authoritative engineering state is
[`../hardware/STATUS.md`](../hardware/STATUS.md) and the regulatory position is
[`../../compliance/COMPLIANCE.md`](../../compliance/COMPLIANCE.md).

**Position taken today:** wellness / research platform, no medical claims.
Changing that position changes every row below, so it must be decided with
counsel before any design freeze.

PDR-02 (Just Works pairing) is **explicitly accepted for the bench prototype**
and remains **Open** for the commercial image.

## Risk register

| ID | Risk | Why it matters for a commercial image | Decision / trigger | Status |
|---|---|---|---|---|
| PDR-01 | OTA is a stub (`synapse_ota.c` has no update path) and `band-product` compiles `FEATURE_OTA=n` | A shipped unit cannot be fixed after deployment; CRA duties start Sept 2026 / Dec 2027 | Signed OTA + rollback test required before any field trial; implement in a dedicated increment after G1 | **Open** |
| PDR-02 | BLE Just Works, `sm_bonding = 0`, no keystore | Config writes are gated on an encrypted link, but the link is not authenticated; residual MITM risk is acceptable on the bench, not in a product | Decide pairing model (bonded LE Secure Connections with display/QR confirmation, or dedicated provisioning) before product build | **Open** |
| PDR-03 | ICM-20948 is EOL (last ship 2026-10-30); MAX30102 has no second source; register-level values (`ppg_sample_rate`, DLPF codes) are in the contract | A lifecycle or supply event can stop production with no drop-in replacement | Define a sensor-abstraction boundary (logical rates/units at the contract, part registers in a driver profile) and qualify one alternative AFE + one IMU | **Open** |
| PDR-04 | LiPo feeds the devkit directly; battery % comes from a divider and a fixed capacity; no charger, fuel gauge, power-path or electrode input protection | Not a manufacturable or safe product design; the 5 mW system target is unmeasured (G3) | Proto-0 hardware spike: PMIC + fuel gauge, input series resistance/ESD, power-path, charging off-body; measure before freezing the enclosure | **Open** |
| PDR-05 | No requirements-to-test traceability matrix, no risk file, no controlled release | Required for ISO 14971 / IEC 62304 if a medical purpose is chosen, and useful evidence for CRA either way | Open a requirements register with IDs linked to G1 test IDs and CI gates; owner: maintainer | **Open** |
| PDR-06 | Product image disables provisioning and OTA; factory mode is off | That is correct for security, but it leaves no ownership/activation path for a sold unit | Decide whether ownership binding happens in the app layer or the band; do not enable factory mode in a product image | **Open** |
| PDR-07 | `cerelog_eeg.py` impedance check is fail-open ("for now") at line 312 | Electrode contact is the first quality signal of a G2 EEG rig; a fail-open check can admit a session recorded through a loose contact | Close it when the Cerelog is on the bench (G2), with a measured impedance threshold and a test that fails on an unreadable channel | **Open — deferred to G2** |
| PDR-08 | `in_ear_satellite` has no real sample source; the coordinator now refuses synthetic in-ear samples unless explicitly constructed as a rehearsal | Fail-closed is correct, but the planned pod cannot produce data until an AFE/transport is chosen | Define the AFE part, the stream contract and the host transport before ordering hardware; the rehearsal flag must never appear in a live run | **Open — deferred to G2** |

## What is deliberately NOT decided here

- PCB placement, enclosure and mechanical design — after G1 numbers exist.
- Certification scope and notch positions — after the intended-use decision.
- Any clinical claim or performance target beyond the research/wellness position.

## Link to the bench

These risks are tracked here so the bench work stays focused: MVP-2.10 makes
the live onboarding path and the per-unit configuration trustworthy; it does **not** claim
that any of the rows above is closed.
