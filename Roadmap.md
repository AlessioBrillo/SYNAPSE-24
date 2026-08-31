# From Zero to a Working Multimodal Bio-Sensing Prototype: A Solo Researcher's Near-Zero-Budget Roadmap (2025–2026)

## TL;DR
- **Start with software and public data, not hardware.** For roughly €0 you can master the entire signal-processing and edge-AI pipeline on validated public datasets (PhysioNet MIT-BIH, Sleep-EDF, EEG Motor Movement/Imagery; WESAD/DEAP for multimodal fusion) using free Python tooling (NeuroKit2, MNE-Python/MNE-NIRS, BioSPPy, WFDB, BrainFlow, Edge Impulse/TensorFlow Lite Micro). Only buy hardware once your algorithms already work on data.
- **A genuine minimum viable multi-signal rig costs ~€90–160.** Cheap breakouts (AD8232 ECG ~€3–7, MAX30102 PPG ~€2–6, MPU-9250/ICM-20948 IMU ~€6–8, ESP32 ~€8, Pi Pico ~€5, breadboard/wires/electrodes ~€30–40) get you ECG+PPG+IMU immediately. Research-grade EEG/fNIRS is the expensive jump: a real EEG channel means either the OpenBCI Ganglion ($624.99), a PiEEG shield (~$350) plus a Raspberry Pi, a Cerelog ESP-EEG ($349.99), or a DIY ADS1299 build.
- **Be honest about the timeline.** For one person working part-time, expect ~1 month to a solid software foundation, ~2–5 months to individual working single-modality prototypes, ~5–12 months to synchronized multimodal capture, and ~12–24+ months before an edge-AI fusion prototype is meaningful. fNIRS and dry-electrode 24/7 EEG are the hardest parts and should be deferred to later phases.

## Key Findings

1. **The cheapest, fastest progress is entirely in software.** Every core biosignal-processing library is free and open-source, and large validated datasets exist for every modality you care about except a perfectly matched all-in-one wearable stream. You can build and benchmark your entire pattern-recognition layer before spending a euro on hardware.

2. **ECG, PPG, and IMU are effectively "solved" cheaply.** Sub-€10 breakout boards (AD8232, MAX30102, MPU-9250/ICM-20948) plus a €5–10 microcontroller give research-usable signals for these three modalities today. These are where a solo builder should get the first end-to-end "sensor → microcontroller → PC → algorithm" loop working.

3. **EEG and fNIRS are the cost-and-difficulty walls.** Quality EEG requires a 24-bit AFE (TI ADS1299 class). Your realistic options are: OpenBCI Ganglion (4-ch, $624.99) or Cyton (8-ch, $1,249), the much cheaper PiEEG (~$350) or Cerelog ESP-EEG ($349.99) ADS1299 boards, a consumer Muse S (~$295–495) for forehead sleep EEG+PPG, or a full DIY ADS1299 build. fNIRS has no cheap commercial entry — the realistic path is an open-source DIY build (DIY-fNIRS headband ~$215, OpenNIRScap ~$419) and it should be the last modality you attempt.

4. **The "sensor pod / hub" architecture maps cleanly onto off-the-shelf parts.** ESP32 (BLE/Wi-Fi, ~€8) or Raspberry Pi Pico (~€5) as pod microcontrollers; Raspberry Pi 4/5 as the compute hub; Lab Streaming Layer (LSL) as the software backbone that synchronizes all streams to millisecond precision across devices. This is the single most important design decision for a multimodal system and it is free.

5. **A single validated multimodal wearable already exists as a shortcut/benchmark: EmotiBit (~$500–549).** It streams PPG (MAX30101, 3-wavelength), EDA, 9-axis IMU, and a medical-grade thermopile (MLX90632), is open-source and BrainFlow/LSL-compatible. Its developer validation study (Chen, Montgomery, Nair & Dikker, "Validating EmotiBit, an open-source multi-modal sensor…", HardwareX/ScienceDirect, article S2665917424000515) benchmarks it against a Brain Products LiveAmp — reporting accelerometer correlation r=0.73 and EDA r=0.83 (both p<0.01) — and OpenBCI markets it as validated versus "a $20,000 gold standard Brain Products device." It won't give you EEG or fNIRS, but it can compress months of PPG/IMU/temperature hardware work and serve as a ground-truth reference for your own DIY boards. **Independent caveat:** a separate study (Vorreuther, Tagalidou et al., *Frontiers in Neuroergonomics* 2025, DOI 10.3389/fnrgo.2025.1585469; n=15 vs BITalino PsychoBit) found EmotiBit heart-rate agreement within 1–2 bpm but judged agreement "insufficient" for HRV and EDA/SCR — so trust it for HR/movement/temperature, but validate HRV and EDA claims carefully.

## Details

### 1. Very first steps (before any code or hardware) — Phase 0

**Step 1 — Literature and reference-architecture review, in this order:**
- **Start with the open-hardware reference designs**, because they encode thousands of hours of hard-won engineering. Read the OpenBCI documentation and the validation literature: Frey (2016), "Comparison of an open-hardware electroencephalography amplifier with medical grade device in brain-computer interface applications," plus a 2026 study (MDPI *Sensors* 26(4):1153) which found "the Cyton records highly similar but not identical scalp EEG as research-grade equipment" (compared against a Brain Products BrainAmp). Study the PiEEG design (ADS1299 + Raspberry Pi, published as "Raspberry Pi Shield – for measuring EEG (PiEEG)"), and read the EmotiBit validation papers for a blueprint of a validated multimodal wearable (MAX30101 PPG, MLX90632 medical thermopile, Bosch 9-axis IMU).
- **Then the modality-specific primers**: for fNIRS, the openfnirs.org ecosystem, the DIY-fNIRS headband paper (HardwareX, 2021, Tsow et al., Vanderbilt/Stanford) and the OpenNIRScap paper (arXiv 2505.20509, 2025). For EEG electrodes, read the DIY dry-electrode literature (e.g., 3D-printed gold-plated pin electrodes, arXiv 2201.03612).
- **Then the software/dataset landscape**: NeuroKit2 paper (Makowski et al., 2021, *Behavior Research Methods*, DOI 10.3758/s13428-020-01516-y), the WESAD dataset paper (Schmidt, Reiss, Duerichen, Marberger & Van Laerhoven, ICMI 2018, DOI 10.1145/3242969.3242985), and the LSL paper (Kothe, Shirazi, Stenner, Medine, Boulay, Grivich, Artoni, Mullen, Delorme & Makeig, "The Lab Streaming Layer for Synchronized Multimodal Recording," *Imaging Neuroscience* 3:IMAG.a.136, DOI 10.1162/IMAG.a.136, published 12 Sept 2025).

**Step 2 — Join the communities before building:** OpenBCI forum, openfnirs.org forum, r/BCI and DIY biosignal communities, the NeuroKit2 and MNE GitHub discussions, and the BrainFlow community. These are where component substitutions, noise-fighting tricks, and dead-ends are documented.

**Step 3 — Set up the free software stack and pull public datasets** (details in §4). Get NeuroKit2, MNE-Python, BrainFlow, and WFDB installed and load real ECG/EEG within your first week.

**Step 4 — Define your "signal quality" yardstick early.** Decide up front how you will quantify SNR, motion-artifact robustness, and (for EEG) electrode impedance, because "high signal quality" is your stated core value proposition and you need a metric before you can improve it.

### 2. Cheapest viable path per modality

Prices are realistic 2025–2026 EU street prices (VAT included where applicable); generic modules are AliExpress/Amazon.de/eBay, branded from Mouser/DigiKey/BerryBase/SparkFun resellers.

**ECG (single-lead):**
- **AD8232 breakout** — generic clone **€3–7** (eBay ~€3.11, AliExpress ~€3.40); SparkFun original (SEN-12650) **~€33.75** at Opencircuit (NL), US MSRP $19.95. The generic clone is fine to start; pair with Ag/AgCl snap electrodes. Analog output into any ADC; the de-facto DIY ECG front-end.

**PPG / HRV:**
- **MAX30102 breakout** — generic **€2–6** (eBay ~€2.10). Integrated red/IR LEDs + photodetector, I²C. Best cheap entry for PPG/HR/SpO2/HRV. (ProtoCentral's quality-controlled "Pulse 3+" version is ~€22.)
- **MAX30101** (used by EmotiBit) is a higher-quality 3-wavelength variant.
- **MAX86150 (combined PPG + single-lead ECG, synchronized)** — ProtoCentral board ~€43–45 but **now discontinued / out of stock** (confirmed on the product page); source generic MAX86150 clones on AliExpress or fall back to separate AD8232 + MAX30102. The MAX86150's synchronized PPG+ECG enables pulse transit time (an indirect blood-pressure proxy).
- **MAX86141** — dual-channel optical AFE (research-grade wrist PPG); available mainly as the Analog Devices evaluation system, more expensive.

**IMU (9-axis):**
- **MPU-9250 / GY-9250** — generic **€6–8**. Widely supported on ESP32/Arduino.
- **ICM-20948** (the MPU-9250's actively-supported successor) — generic €10–15, branded SparkFun/Adafruit ~€17–25 (BerryBase). Prefer ICM-20948 for new designs since the MPU-9250 is discontinued by the manufacturer.

**EEG (the expensive decision):**
- **Cheapest "real" route — PiEEG 8-channel shield: ~$350** (≈€325) via Elecrow, + a Raspberry Pi (Pico won't do; needs Pi 4/5). 8-channel, ADS1299, 24-bit, 250 SPS–16 kSPS, BrainFlow-compatible. PiEEG-16 is ~$390.
- **Cerelog ESP-EEG: $349.99** — 8-channel ADS1299 with ESP32 (Wi-Fi/USB-C) + LiPo charging, closed-loop active bias (Driven Right Leg), BrainFlow/OpenBCI-GUI/LSL compatible. Strong value and closest to your "sensor pod" ESP32 vision.
- **OpenBCI Ganglion: $624.99** (4-ch) or **Cyton: $1,249** (8-ch) — the gold-standard validated open hardware, but expensive and US-shipped (OpenBCI itself warns EU import tax can reach ~25%). Electrodes sold separately.
- **Consumer shortcut — Muse S / Muse S Athena: ~$295–495** — dry-electrode forehead EEG + PPG + IMU (and, in the Athena, an fNIRS channel), with an SDK exposing raw EEG/PPG/accelerometer; excellent for the continuous low-power "Tier 0" forehead sleep-EEG role. SDK is free for prototyping/personal use (commercial requires a license).
- **DIY ADS1299 build** — cheapest in raw parts but the ADS1299 chip is costly/hard to source and requires serious PCB skills; not recommended as a first build.
- **Electrodes:** gold cup EEG electrodes (pack of 10) **€15–33** (OpenBCI ~€28–33); reusable/disposable Ag/AgCl snap ECG electrodes 50-pack **€8–15**. Dry electrodes can be DIY'd (3D-printed gold-plated pins).

**fNIRS (defer to last):**
- No cheap commercial option. **DIY-fNIRS headband ~$215** (4 detectors, single LED pair, 10 Hz, open-source PCB/BOM on OSF, HardwareX 2021). **OpenNIRScap ~$419** (24-channel, dual-wavelength, 1 kHz, fully open-source, GitHub tonykim07/fNIRS). **NIRDuino** and **NinjaNIRS** are other open modular designs. Expect real analog/optical engineering effort.

**Microcontrollers / compute hub:**
- **ESP32 DevKit** — **€6–12** (BerryBase €7.68). BLE + Wi-Fi, ideal wireless "sensor pod" brain and Edge Impulse/TFLM target.
- **Raspberry Pi Pico / Pico 2** — **€4–7**. Ultra-cheap, low-power pod controller.
- **Raspberry Pi 4 (4GB) ~€60–70 / Pi 5 (4GB) ~€60–65** — the compute "hub" for sensor fusion, LSL aggregation, and heavier on-device ML; also required for PiEEG. (Note: BerryBase currently lists a Pi 4 4GB at €105.90 with low stock — anomalously high vs. the ~€60–70 market norm.)
- **Prototyping supplies:** 830-point breadboard €3–5; Dupont jumper kit €6–9; full Elegoo-style starter kit €15–25.

### 3. Itemized budget

**Phase 1 — Absolute-minimum ECG+PPG+IMU rig (no research-grade EEG yet):**
| Item | EUR |
|---|---|
| AD8232 ECG breakout (generic) | 5 |
| MAX30102 PPG breakout (generic) | 5 |
| MPU-9250 / ICM-20948 IMU | 8 |
| ESP32 DevKit | 8 |
| Raspberry Pi Pico (spare pod MCU) | 5 |
| Ag/AgCl snap ECG electrodes (50-pack) | 12 |
| Breadboard + jumper wires + starter kit | 20 |
| Misc (LiPo, connectors, USB cables) | 20 |
| **Subtotal** | **~€83** |

Add a **DIY few-channel EEG experiment** with gold cup electrodes into an ESP32/ADS1115-class ADC only as a crude teaser (real EEG needs ADS1299). Realistic **all-in minimum: ~€90–120.**

**Phase 2 — Credible multimodal research rig:**
| Item | EUR |
|---|---|
| PiEEG 8-ch shield ($350) or Cerelog ESP-EEG ($349.99) | ~325 |
| Raspberry Pi 5 (4GB) for hub / PiEEG | 65 |
| Gold cup EEG electrodes (10-pack) | 25 |
| EmotiBit (validated PPG/EDA/IMU/temp reference, ~$500–549) — optional but high-leverage | ~470 |
| Muse S (Tier-0 continuous sleep EEG/PPG) — optional | ~300 |
| Extra breakouts, LiPos, 3D-printed enclosures, misc | 80 |
| **Subtotal (core, without optional items)** | **~€420** |
| **With EmotiBit + Muse S** | **~€1,190** |

**Phase 3 — fNIRS + polish:** DIY-fNIRS (~$215) or OpenNIRScap (~$419) parts, better electrodes, custom PCBs, soldering/rework tools if not owned. Budget **€300–700** more.

**Bottom line:** A meaningful ECG+PPG+IMU prototype is achievable for **under €120**. A credible *multimodal including real EEG* rig lands around **€400–500**. Adding validated references (EmotiBit) and consumer EEG (Muse S) roughly **€1,200**. fNIRS pushes toward **€1,500–2,000** total.

### 4. Software stack and order of operations

**Acquisition & preprocessing (free, Python):**
- **NeuroKit2** — the best single starting point; high-level ECG/PPG/EDA/EMG/RSP processing, HRV, plus built-in signal simulators (`ecg_simulate`, `ppg_simulate`) so you can generate synthetic data before any hardware.
- **BioSPPy** — broad biosignal processing, 200+ features.
- **MNE-Python** — the standard for EEG (and fNIRS via **MNE-NIRS**); filtering, ICA artifact removal, epoching, decoding. Reads EDF/SNIRF.
- **WFDB** — read/write PhysioNet formats (MIT-BIH etc.).
- **HeartPy**, **pyHRV**, **YASA** (sleep staging), **NeuroDSP** — specialized add-ons.

**Hardware acquisition SDKs:**
- **BrainFlow** — board-agnostic acquisition + signal-processing API (Python/C++/others), supports OpenBCI Cyton/Ganglion, PiEEG, Cerelog ESP-EEG, Muse, EmotiBit; write code once and swap boards. Its **Synthetic Board** and **Playback Board** let you develop with no hardware.
- **OpenBCI GUI** — quick visual sanity-checking.
- **Lab Streaming Layer (LSL)** — the backbone for your multimodal/sensor-pod architecture: per-sample timestamps, millisecond sync across independent devices/clocks, records to XDF; supported by MNE-Python, BrainFlow, Muse, EmotiBit, OpenViBE, Timeflux. This is how you fuse ECG+PPG+EEG+IMU+fNIRS streams cleanly.

**Datasets to prototype algorithms on (all free):**
- **ECG:** MIT-BIH Arrhythmia, **PTB-XL** (Wagner, Strodthoff, Bousseljot et al., 2020, *Scientific Data*, DOI 10.1038/s41597-020-0495-6 — "21837 records from 18885 patients of 10 seconds length," 52% male/48% female, ages 0–95 (median 62), recorded on Schiller AG devices Oct 1989–Jun 1996), MIT-BIH Supraventricular.
- **EEG:** EEG Motor Movement/Imagery Dataset (109 subjects, 64-ch, 160 Hz, on PhysioNet; a cleaned CSV/MATLAB curation of 103 subjects exists — Shuqfa, Lakas & Belkacem, DIB 2024, DOI 10.1016/j.dib.2024.110181); Sleep-EDF Expanded (197 whole-night PSGs).
- **PPG/wearable:** "Motion and heart rate from a wrist wearable + polysomnography" (Apple Watch sleep dataset on PhysioNet).
- **Multimodal fusion (most relevant to you):** **WESAD** (Schmidt et al., ICMI 2018 — 15 subjects; chest device = RespiBAN with ECG/EDA/EMG/RESP/temp/3-axis ACC at 700 Hz, wrist = Empatica E4 with BVP/PPG, EDA, temp, ACC; benchmark accuracy up to 80% for 3-class and 93% for binary stress-vs-non-stress; hosted on UCI) and **DEAP** (EEG + peripheral physiology). These let you build and validate sensor-fusion models *now*.
- **fNIRS:** "Motion Artifact Contaminated fNIRS and EEG Data" (PhysioNet) plus openfnirs sample data.

**Edge AI / TinyML:**
- **Edge Impulse** — best solo-developer on-ramp: browse/collect data, train, quantize (EON compiler), deploy as an Arduino/ESP32 library or C++ SDK; imports ONNX/TF models.
- **TensorFlow Lite for Microcontrollers (TFLM)** — the deployment runtime on ESP32/Cortex-M; pairs with CMSIS-NN.
- **ONNX Runtime** — for the Raspberry Pi hub (heavier models).
- For your spiking-neural-net ambition, prototype on the hub first; SNN-on-MCU tooling is immature and should be a research spike, not a critical path.

**Recommended order of operations:**
1. **Weeks 1–4:** Install stack; load MIT-BIH + Sleep-EDF; reproduce ECG R-peak/HRV and EEG band-power/sleep-staging pipelines in NeuroKit2/MNE. Use BrainFlow Synthetic Board.
2. **Months 2–3:** Build a **sensor-fusion model on WESAD** (e.g., stress/affect classification from ECG+PPG+EDA+ACC). This exercises the entire fusion + personalization + evaluation pipeline with zero hardware.
3. **Months 3–4:** Take one trained model through Edge Impulse → TFLM → ESP32 to prove the edge-AI deployment loop (even on accelerometer data first).
4. **Then** integrate live hardware modality-by-modality, always comparing against your dataset-trained baselines and (if bought) EmotiBit ground truth.

### 5. Sequencing and milestones (solo, realistic)

- **Phase 0 — Foundations (Weeks 1–4, ~€0):** literature/reference-design review, communities, software stack, first dataset pipelines running. *Milestone: reproduce a published ECG-HRV and an EEG sleep-staging result on public data.*
- **Phase 1 — Software mastery + first cheap hardware (Months 2–5, ~€90–120):** WESAD fusion model; edge-AI deployment loop; first live ECG (AD8232), PPG (MAX30102), IMU (MPU-9250/ICM-20948) into ESP32 → PC via LSL. *Milestone: live ECG+PPG+IMU streaming, synchronized in LSL, with real-time HR/HRV.*
- **Phase 2 — Real EEG + synchronized multimodal (Months 5–12, +€300–500):** add PiEEG/Cerelog/Ganglion EEG; consider EmotiBit as reference and Muse S for Tier-0 continuous sleep EEG; get all modalities co-recording in XDF. *Milestone: a night of synchronized multimodal sleep data (EEG + PPG + IMU + temp) you can analyze end-to-end.*
- **Phase 3 — Edge-AI fusion + fNIRS (Months 12–24+, +€300–700):** port fusion/personalization models to the ESP32 pods + Pi hub; attempt a DIY-fNIRS or OpenNIRScap channel for Tier-1 sessions; iterate on dry-electrode signal quality and 24/7 wearability. *Milestone: an on-device edge-AI pattern-recognition demo running on your own multimodal stream.*

**Honest reality check:** 24/7 dry-electrode EEG at good signal quality is an unsolved problem even for funded labs; fNIRS DIY is genuinely hard analog/optical work; and long-term wearability/power are their own multi-month efforts. A solo, part-time researcher should treat the ECG+PPG+IMU+consumer-EEG stack as the achievable near-term win and treat dense dry-EEG and DIY-fNIRS as ambitious research spikes, not deliverables on a fixed schedule.

## Recommendations

1. **Do Phase 0 and most of Phase 1 in software before spending money.** Your stated core value proposition is the edge-AI/fusion layer — build and validate it on WESAD/DEAP/PhysioNet first. This de-risks everything and costs €0. *Threshold to proceed to hardware: you can reproduce published ECG, EEG, and multimodal-fusion results and have a working Edge Impulse→ESP32 deployment.*
2. **Buy the ~€90 ECG+PPG+IMU kit first** and get a synchronized LSL stream working. This teaches you real-world noise/artifact/power problems cheaply. *Threshold to escalate: stable, artifact-managed multi-signal capture with sensible HR/HRV.*
3. **For EEG, buy the Cerelog ESP-EEG ($349.99) or PiEEG ($350) rather than OpenBCI** unless you specifically need OpenBCI's validation pedigree — both are ADS1299/BrainFlow-compatible at roughly a third of the Cyton price, and the Cerelog's ESP32 fits your sensor-pod architecture. Consider a **Muse S** for the continuous low-power Tier-0 sleep role. *Threshold: only after Phase 1 is solid.*
4. **Strongly consider one EmotiBit** as a validated multimodal reference for PPG/IMU/temperature. At ~$500–549 it can save months and gives you ground truth to validate your DIY boards against — but treat its HRV and EDA outputs with caution given the independent Frontiers study's "insufficient" agreement finding. Treat it as measurement infrastructure, not a crutch.
5. **Defer fNIRS to Phase 3.** Build one DIY-fNIRS or OpenNIRScap channel only after your EEG + peripheral stack is stable; it is the highest-effort, lowest-maturity modality.
6. **Adopt LSL/XDF from day one** as your synchronization and storage standard — retrofitting sync later is painful.
7. **Keep a written signal-quality metric and a lab notebook/dataset from your very first recording** so your personalization/edge-AI work has clean, labeled, provenance-tracked data.

## Caveats

- **Prices shift and vary by region.** Generic AliExpress/eBay module prices exclude shipping (~€5–7 from China); OpenBCI/PiEEG/Cerelog/Muse ship from the US and add EU import VAT/duty (OpenBCI itself warns of up to ~25%). BerryBase's Pi 4 4GB at €105.90 looked anomalously high vs. a ~€60–70 market norm, and several sub-€10 module prices are indicative ranges rather than fixed quotes because generic-module pricing is volatile.
- **The ProtoCentral MAX86150 combined PPG+ECG board is discontinued/out of stock** — plan on generic clones or separate AD8232+MAX30102.
- **"Not a medical device."** Every board here (OpenBCI, PiEEG, Cerelog, Muse, EmotiBit) explicitly disclaims medical use — appropriate for your stated experimental/hobbyist phase, but do not draw clinical conclusions.
- **Validation is not identity.** Even the well-regarded Cyton "records highly similar but not identical scalp EEG" vs. research-grade amplifiers (MDPI *Sensors* 2026), and EmotiBit's HRV/EDA agreement is contested — so calibrate expectations for any low-cost device.
- **Consumer-device SDK licensing:** the Muse SDK is free for prototyping/personal use but requires a commercial license if you later monetize; factor this into any future pivot.
- **Signal quality is the hard part, not connectivity.** Getting a clean EEG or fNIRS signal on a moving, awake person 24/7 is dramatically harder than getting a demo trace at a desk. Budget most of your time (not money) here.