# backend: the local Data Plane

A FastAPI service that serves the session catalog, reports and job runs of this repository's
recordings on loopback. It is **not** the product backend: accounts, consent and the system of
record are Averyn's, and nothing here draws a chart (the local viewer was removed; ADR-0030 in
the Averyn repository puts every view of the band's data there).

It is in the gate: `mypy` covers it and the coverage figure includes it (`tests/test_backend_*.py`,
`tests/test_docs_interface.py`). The contract it exposes is `docs/contracts/AVERYN_INTERFACE.md`.
Endpoints that need a live device (`/api/live/*`, `/api/control/*`) answer 501 on purpose.
