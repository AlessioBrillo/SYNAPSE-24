# synapse-device

Host SDK for the Synapse Band v1: discover, record, stream, export and analyze
bio-sensing sessions (ECG, PPG, IMU, GPS, temperature, power).

## Install

```bash
pip install synapse-device          # from PyPI (coming)
pip install -e sdk/python           # from this repo
```

## CLI

```bash
synapse --version
synapse discover                    # synthetic pod when no BLE adapter
synapse info
synapse record --source synthetic --out session.xdf --duration 30
synapse index ./sessions
synapse report session.xdf
synapse analyze session.xdf --job hrv_trends
```

## License

Apache-2.0. This SDK deliberately never imports the AGPL-licensed `synapse24`
package, so `synapse-device` can stay permissive while `synapse24` depends on
it.
