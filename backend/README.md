# backend — STATUS: experimental, not verified in CI

This FastAPI service is a **developer convenience surface** for the frontend,
not part of the G1 critical path. It has no test job in CI and is not
exercised by the hardware-readiness gates.

Options are tracked (same as `frontend/README.md`): bring into CI with a
minimal test job, or remove it once the live stack proves out. Do not treat
anything in here as validated infrastructure until then.
