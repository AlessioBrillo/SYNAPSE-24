# synapse-device

Device layer for the Synapse Band v1: discover, record and inspect
bio-sensing sessions (ECG, PPG, IMU, GPS, temperature, power), and read and
write the XDF and FIT formats.

This package is Apache-2.0 and deliberately never imports the AGPL-licensed
`synapse24` package: the *device layer* lives here, and the host pipeline
(analysis, catalog, report) depends on it, never the other way round. The
format writers (`synapse_device.fit`, `synapse_device.xdf_writer`) are the
single implementations used by the export script and by the worker.

## Install

```bash
pip install synapse-device          # from PyPI (coming)
pip install -e sdk/python           # from this repo
```

## CLI

```bash
synapse --version
synapse discover                    # scan for pods (BLE adapter required)
synapse info                        # device info / battery / sensors
synapse record --source synthetic --out session.xdf --duration 30
synapse inspect session.xdf          # streams of an XDF, records of a FIT
```

The analysis side of the loop (session catalog, SessionReport, worker jobs,
XDF→FIT export) lives in the host pipeline and has its own entry points:
`synapse-sessions`, `synapse-report` and `synapse-export`. The SDK CLI says
so (and exits with code 2) if one of the old commands is used.

## License

Apache-2.0. This SDK deliberately never imports the AGPL-licensed `synapse24`
package, so `synapse-device` can stay permissive while `synapse24` depends on
it.
