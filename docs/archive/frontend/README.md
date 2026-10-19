# frontend — STATUS: experimental, not verified in CI

This React/Vite dashboard (pod manager, live signals, tier control) is a
**developer convenience surface**, not part of the G1 critical path. It has
no test job in CI and is not exercised by the hardware-readiness gates.

Two options are allowed, both tracked:
1. bring it into CI (`build` + `lint` at minimum) once the live stack is
   stable and there are real streams to render;
2. remove it after the live stack proves out.

Do not treat anything in here as validated infrastructure until then.
