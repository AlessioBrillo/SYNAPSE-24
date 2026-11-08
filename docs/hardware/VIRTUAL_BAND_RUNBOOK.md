# Virtual band runbook

A PC that behaves like a Synapse band towards a phone, to see what Averyn really does on the radio
before a real band exists. It replays `contracts/fixtures/session_60s.*` (a synthetic minute, nothing
measured) through the real GATT table of `contracts/gatt_v2.json`.

**What it proves:** the phone's side of the conversation (scan, MTU, subscriptions, `set_time`,
the raw gate, reconnects) and that what the phone stores is what was sent.
**What it does not prove:** the band's firmware, its sensors, its radio, or iOS.

## Needs

- A host whose Bluetooth adapter works as a **peripheral**. The built-in Realtek adapter of the
  first dev PC does not (Windows aborts every connectable advertisement); use a USB BLE dongle
  (disable the built-in adapter) or, later, a Mac/BlueZ host.
- An Android phone with **USB debugging** and a **debug build** of Averyn (`run-as` needs it):
  `./gradlew :apps:android:installDebug` in the Averyn repository.
- In Averyn: the band selected in the settings and the consents you want to test granted
  (signal quality, raw signals). Raw uploads need no setting for this test.

## Steps

1. Is the radio usable at all? `uv run python -u scripts/virtual_band.py --probe`, then look for the
   consumer service `f9e08ffd-3d34-441f-97a6-1bf0958123f1` in nRF Connect. Stop here if it is not
   there; `adv_status` `3` in the log means the adapter refused to advertise.
2. Start a scenario: `uv run python -u scripts/virtual_band.py --scenario clean --log band.jsonl`.
3. In Averyn start an activity; let it run for about 70 s (the program is 60 s); stop it.
4. `uv run python scripts/check_phone_capture.py pull --out capture/`
5. `uv run python scripts/check_phone_capture.py check --band-log band.jsonl --features capture/<id>.synapse.bin --raw capture/<id>.synapse-raw.bin`
   (add `--no-raw-signals` / `--no-signal-quality` when that consent was not given).

Exit 0 = every sidecar is a byte-for-byte slice of what the band delivered, the phone sent
`set_time` on every connection, subscribed as the consents allow and got an MTU that fits a feature
frame. `note` lines are observations (subscription order, retries, frames missed at the start).

## Scenarios

| `--scenario` | The band does | The phone should |
|---|---|---|
| `clean` | plays the minute | record it, send `set_time`, subscribe feature, status and raw |
| `disconnect` | drops the link at 20 s, advertises again at 25 s | reconnect, send `set_time` again, keep recording; the 5 s gap shows as lost frames |
| `restart` | at 30 s every raw stream starts again from sequence 0 | count a restart, not garbage |
| `cp_error` | refuses the first `set_time` with `bad_argument` (ATT `0x80+3`) | not crash; whether it retries is noted |

`disconnect` is best effort: Windows has no server-side disconnect, so the band closes its GATT
sessions and withdraws the advertisement. If the phone stays connected, the log shows
`drop_link` without a second `connect`.

## What to write down

Per scenario: the checker output, `band.jsonl` (events only, not the binaries), the phone model and
Android version. Anything Averyn does wrong becomes Averyn debt with the steps to reproduce; this
repository does not fix it.
