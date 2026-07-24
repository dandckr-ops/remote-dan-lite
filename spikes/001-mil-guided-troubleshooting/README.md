# Spike 001 — MIL-Aware Guided Troubleshooting

## Question

Given a confirmed emissions DTC and MIL/readiness context, can Remote Dan Lite present a technician-usable path from fault context through bounded OBD evidence to an honest PicoScope handoff without pretending generic OBD knows vehicle-specific wiring?

## Run

```bash
cd /tmp/rdl-guided-spike-ares
python3 -m http.server 8877 --bind 127.0.0.1
```

Open `http://127.0.0.1:8877/`.

## Scenario

The mock uses the sanitized Subaru evidence shape already observed by RDL:

- MIL off
- three confirmed/stored generic emissions DTCs
- P0102 selected as the primary workflow
- P0113 retained as related MAF/IAT circuit evidence
- P0028 retained as a separate diagnostic branch

All PID values, trend lines, decisions, and scope setup states are mock data. There are no network calls, backend routes, adapter commands, or Mode $04 controls.

## What to evaluate

1. Is the relationship between MIL state, stored status, and fault priority understandable?
2. Does the OBD event sequence feel like a real diagnostic test rather than a dashboard?
3. Is the MAF dual-unit presentation useful?
4. Is the transition from ECU-reported values to electrical measurement clear?
5. Does the pinout blocker feel honest rather than obstructive?
6. Is the evidence package a useful end state?

## Boundary

This is disposable interaction code under `spikes/`. It must not be promoted directly into production. A real implementation needs versioned playbooks, Mode $02 support, bounded time-series evidence, real Pico preset APIs, vehicle overlays, persistence, and backend-enforced authority boundaries.

## Verdict: VALIDATED AS AN INTERACTION MOCK

### What worked

- A confirmed DTC can launch a focused five-stage workflow without turning the fault code into a parts recommendation.
- MIL state, DTC state, related-code context, and the first history question remain visible before testing.
- KOEO, warm idle, 2,500 RPM, and throttle-transition events update a bounded PID set and accumulate nine evidence observations.
- MAF dual units (`lb/min` and `g/s`) fit cleanly without weakening the imperial operator surface.
- An inconclusive OBD observation routes explicitly to a PicoScope signal/ground/supply preset.
- The scope handoff remains blocked from inventing connector pins or wire colors.
- Desktop and 390 px browser runs completed the whole path with no JavaScript errors or document-level horizontal overflow.

### What did not become real

- PID values and trend shapes are illustrative mock data.
- There is no Mode $02 acquisition, live OBD recorder, Pico API call, persistence, or report generator.
- The fault-card selector demonstrates hierarchy but only P0102 has a playbook.
- Vehicle-specific connector and threshold overlays remain deliberately absent.

### Surprises

- The workflow's `hidden` state initially leaked because grid CSS overrode the HTML attribute.
- The mobile stepper needed explicit containment and active-stage scrolling.
- The long masthead word, not the data grid, caused the final narrow-screen overflow.

### Recommendation for the real build

Keep this interaction model, but implement one vertical production slice rather than a generic playbook platform: P0102/P0113 context resolution, bounded relevant-PID recording with event markers, deterministic technician decisions, a real Pico preset handoff, and evidence persistence pinned to a playbook revision. Do not promote this mock code directly into the runtime.
