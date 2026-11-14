# Increment: MVP-1.7 — la band virtuale su PC (per provare il telefono via radio)

**Periodo:** 2026-11-08/09 · **Stato:** **parzialmente chiuso**. La parte senza radio è su `master` e testata; **la prova via radio (PR-4) non è stata fatta** perché il PC di sviluppo non può fare da periferica BLE. Resta in attesa di un dongle USB BLE.

Questo documento non esisteva: l'incremento era citato solo in `MVP1_8_REAL_SIGNAL.md`, in `FIRST_HARDWARE_DAY.md` e nel runbook. Lo scrive la chiusura di MVP-1.9.

## Scopo

Vedere cosa fa davvero Averyn sulla radio (scansione, MTU, sottoscrizioni, `set_time`, il cancello dei dati raw, le riconnessioni) prima che esista una band vera: un PC che si comporta da band verso un telefono, rigiocando `contracts/fixtures/session_60s.*` attraverso la tabella GATT reale di `contracts/gatt_v2.json`. Solo SYNAPSE-24; Averyn non si modifica.

## Fatto (senza radio, su `master`: #87, #88, #89)

| PR | Cosa | Dove |
|---|---|---|
| PR-1 (#87) | Codec nell'SDK: `parse_control` / `ControlError` (le stesse regole e lo stesso ordine di controlli di `synapse_control_apply`) ed `encode_hr_measurement`; parità con il C | `sdk/python/synapse_device/consumer.py`, `tests/test_consumer_v2_parity.py` |
| PR-2 (#88) | Il nucleo della band virtuale: tabella GATT dal contratto, `load_program`, `BandSession` con gli scenari `clean`, `disconnect`, `restart`, `cp_error`; il binding WinRT | `scripts/virtual_band_core.py`, `scripts/virtual_band.py`, `tests/test_virtual_band.py` |
| PR-3 (#89) | Il controllo di ciò che il telefono ha registrato: una slice contigua di ciò che la band ha consegnato (pull via adb + check) | `scripts/check_phone_capture.py`, `tests/test_check_phone_capture.py` |
| Doc | Il runbook | `docs/hardware/VIRTUAL_BAND_RUNBOOK.md` |

## NON fatto

- **PR-0 / PR-4, la radio.** La sonda (`virtual_band.py --probe`) ha mostrato che il PC non può fare da band: l'adattatore Realtek USB (VID_13D3 PID_3600) dichiara il ruolo periferico, ma ogni advertising connettibile finisce `Aborted` (status 3), anche con un solo servizio. Il binding WinRT di `virtual_band.py` **non è mai stato provato via radio**. `0x180A` (Device Information) è rifiutato da Windows (`DisabledByPolicy`): sul PC la band virtuale non ha Device Information, che Averyn legge per il binding.
- Il documento di chiusura con la sessione sulla radio.

## Per riprendere

1. Un dongle USB BLE: disabilitare l'adattatore interno e rilanciare `uv run python -u scripts/virtual_band.py --probe`; poi nRF Connect sul telefono (vedi il runbook).
2. Se il PC resta inadatto: un Mac (`bless` / CoreBluetooth) o BlueZ. `bless` è scartato su Windows: dipende da `bleak_winrt` (nessuna wheel per Python 3.12) e da `pysetupdi`, e scrive in HKLM.
3. Gli output dei comandi lunghi vanno su file con `-u`: `| tail` nasconde tutto se il processo viene ucciso.

## Verifica

Suite completa a blocchi (2026-11-08): 92 file, 0 falliti, 27 saltati per l'ambiente, 83,73 % filtrato. I nuclei puri (`virtual_band_core.py`, `check_phone_capture.py`) sono nella suite; la parte radio è manuale e fuori dal gate (`AGENTS.md`).
