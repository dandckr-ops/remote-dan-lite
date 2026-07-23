# Remote Dan Lite architecture and status boundaries

This document separates product architecture from implementation status. A component can belong in the v1 architecture without being commissioned in the current appliance runtime.

## Status vocabulary

| Status | Meaning |
|---|---|
| **Proven** | Exercised through the real hardware or live service path with inspectable output. |
| **In governed source** | Implemented and tested in Git, but not necessarily deployed to the appliance. |
| **Designed** | Accepted product/interaction architecture that still requires implementation. |
| **Connected satellite** | External hardware is configured and reachable, but the Remote Dan API/UI path may still be pending. |
| **Deferred** | Deliberately outside the current implementation slice. |

At the current repository baseline:

- Pi 5 + PicoScope 2406B network capture is **proven**.
- Passive CAN analysis is **hardware-path proven** at a negotiated 0.104 µs sample interval over all three commissioned channels with no overflow.
- CSV, JSON, PNG, PDF, and checksum artifacts are **proven**.
- SQLite metadata and evidence lineage are **proven live**.
- Seven session-centered tabs are **proven live** and share one evidence state; Serial and guided-test acquisition remain commissioning boundaries.
- Profile-driven Scope acquisition is **hardware-preflight proven** on the real 2406B, including a 10-second capture and model-accepted ranges from ±20 mV through ±20 V.
- The Anybus AB7702 is a **connected satellite**; Remote Dan integration is pending.
- The complete three-device enclosure and independent recovery hardware remain an architecture target.

## Product doctrine

Remote Dan Lite is a synchronized evidence appliance, not an automated diagnostician.

It should:

- keep capture hardware attached to the machine
- keep the technician's laptop available as a normal workstation
- capture bounded, repeatable event windows
- preserve raw artifacts before interpretation
- record derived calculations and confidence separately from operator findings
- correlate evidence from multiple acquisition lanes inside one diagnostic session
- fail closed for unavailable hardware and consequential protocol writes

## Three hardware planes

### Capture plane

Owns:

- PicoScope acquisition
- CAN and serial acquisition adapters as they are commissioned
- artifact generation
- local web service
- SQLite metadata and lineage
- event markers and common session time context
- evidence publication to the operator laptop

The capture plane does not own field routing or independent recovery authority.

### Routing/control plane

Owns:

- predictable service addressing
- direct laptop access
- shop/truck uplink
- target-device adjacency or isolated target segment
- reachability to external protocol satellites

A small dedicated OpenWrt-style device is the default target when these network modes become part of the physical enclosure.

### Recovery/OOB plane

Owns:

- capture-node health checks
- heartbeat monitoring
- bounded reboot or power-cycle authority
- minimal independent recovery status

It should remain simpler than the capture node and should not depend on the capture service to perform recovery.

## Session-centered operator surface

The implemented primary tabs are:

1. **Overview** — hardware readiness, synchronization, storage, capture state, and recent sessions
2. **Scope** — profile-driven physical-signal acquisition with four configurable channels
3. **Serial** — raw serial configuration, capture, and decode evidence
4. **CAN** — listen-only acquisition, battery gauge, CAN-H/CAN-L evidence, passive signal intelligence, and confidence-ranked protocol fingerprints
5. **Tests** — guided workflows that configure the shared acquisition engines
6. **Timeline** — correlated scope, CAN, serial, test, and operator events
7. **Evidence** — session packages, lineage, raw artifacts, calculations, reports, and operator findings

Connections and System remain secondary setup areas.

A tab is a view or configuration surface over the same session. It must not duplicate the scope, CAN, serial, or evidence implementation.

### Scope versus CAN

Scope and CAN deliberately use the same capture/evidence engine but expose different operator contracts:

- **Scope** owns physical-signal profiles, A–D channel enable state and labels, AC/DC coupling, input range, probe ratio, collection window, waveform review, and bounded next-capture auto-range suggestions.
- **CAN** owns the commissioned fixed harness: Channel A VBAT through 20:1, Channel B CAN-H, Channel C CAN-L, passive signal intelligence, bus-derived measurements, and listen-only network evidence.
- Scope and CAN retain independent “latest capture” views so one lane does not overwrite the other operator context.

Current Scope starting profiles are General/custom, Secondary ignition pickup, Crankshaft VR, Crankshaft Hall, and Injector primary. Secondary ignition is pickup-only: the scope, BNC, and ground lead must never connect directly to secondary voltage.

The live 2406B was probed rather than trusting the SDK enum. This unit accepts ±20 mV, ±50 mV, ±100 mV, ±200 mV, ±500 mV, ±1 V, ±2 V, ±5 V, ±10 V, and ±20 V. It rejects the SDK's ±10 mV and ±50 V enum values. With a selected 20:1 attenuator, the maximum displayed full scale is therefore ±400 V; the physical accessory rating still governs.

### Passive CAN analysis boundary

The dedicated CAN analysis window requests 250,000 samples at 0.1 µs. The connected 2406B negotiated 0.104 µs, approximately 9.615 MS/s, while acquiring VBAT, CAN-H, and CAN-L without overflow. This supports strong nominal-rate evidence through 1 Mbit/s and useful 2 Mbit/s CAN FD data-phase evidence. Faster CAN FD phases are reported as unresolved when fewer than four samples per bit are available.

The persisted analysis separates measurement from inference:

- bus load is observed frame occupancy from SOF through the following 11 recessive nominal bit times over the recorded window;
- nominal and data bitrates are selected from standard rates only when edge timing and CAN arbitration headers agree;
- Classical CAN versus CAN FD comes from decoded FDF/BRS header evidence, not voltage shape;
- J1939 and NMEA 2000 require CRC-valid 29-bit Classical CAN frames plus known PGN patterns and plausible standard rates;
- OBD-II/ISO-TP and CANopen require CRC-valid frames plus their request/response or heartbeat/SDO identifier patterns;
- unmatched traffic remains higher-layer unresolved or proprietary CAN;
- Pico overflow, too few samples per bit, no activity, and ambiguous timing fail closed or lower confidence.

The analysis is passive and listen-only. It does not transmit, acknowledge, replay, fuzz, or actively probe the bus. A short observation window describes only traffic seen during that window; absence of CAN FD or a protocol fingerprint does not prove the vehicle or network cannot use it elsewhere.

### Guided tests

Relative compression, cylinder contribution, alternator ripple, injector current, and cam/crank correlation belong under **Tests**, not inside the generic Scope page.

A guided workflow should follow:

```text
Purpose
  → connection instructions
  → signal-quality check
  → armed bounded capture
  → deterministic calculation
  → confidence/limitations
  → operator findings
  → evidence package
```

The system may support a technician's diagnosis, but it should not overstate a calculation as a definitive cause.

## Evidence model

The durable metadata lineage is:

```text
asset
  └── diagnostic case
       └── session
            └── capture
                 ├── artifact
                 ├── channel configuration
                 ├── event marker
                 └── test result
```

SQLite stores local metadata and relationships. It does not store large waveform or report files as database blobs.

Artifacts remain on the filesystem and are indexed by:

- database ID
- capture ID
- kind
- filename
- relative path
- media type
- byte size
- SHA-256 checksum

This keeps the appliance simple and locally recoverable while leaving a clean migration path if a central service is justified later.

## Pico capture boundary

The proven Pico path currently acquires:

- Channel A: VBAT through configured attenuation
- Channel B: CAN-H
- Channel C: CAN-L

It derives:

- per-channel min/max/mean/peak-to-peak/standard deviation
- B-minus-C differential
- CAN common mode
- CAN-H/CAN-L correlation

Raw samples stay in CSV. Summary values stay in JSON and the manifest. The browser may present VBAT as a two-decimal digital value while preserving the full VBAT waveform in raw evidence.

Hardware readiness requires more than importing `picosdk`:

1. matching native PS2000A library
2. correct CPU ABI
3. USB enumeration and permissions
4. successful open-unit probe
5. bounded acquisition
6. artifact generation
7. artifact download and checksum verification

## Modbus satellite boundary

The Anybus AB7702 is external to the Remote Dan enclosure. It bridges structured Modbus TCP transactions to the configured RS-485 Modbus RTU field side.

The intended path is:

```text
operator browser
  → authenticated Remote Dan API
  → private LAN
  → Anybus gateway
  → RS-485 Modbus RTU field device
```

Initial integration must be read-only.

Every request should record:

- session and operator context
- gateway identity
- unit/target ID
- function code
- address and quantity/range
- decoded values and raw response where appropriate
- start/end time and duration
- timeout/protocol/exception classification

The gateway is not raw serial evidence. It cannot prove line levels, framing timing, CRC corruption on failed frames, collisions, or non-Modbus traffic. Those require a direct isolated serial adapter or scope tap.

Future writes must require a separate, explicit, bounded unlock with visible target/function/address scope. Do not expose Modbus TCP/502 through public ingress.

## CAN authority boundary

CAN starts listen-only. Active transmission, replay, fuzzing, or stimulus does not belong in the ordinary CAN tab.

If added later, active operations require a separately armed **Stimulus** mode with:

- explicit target/bus selection
- bounded message allowlist
- visible timing and count limits
- operator confirmation
- complete action logging
- fail-closed cancellation and timeout behavior

## Power and connector boundary

The enclosure target supports protected bench AC and 12/24 VDC input feeding a regulated internal bus. The design must account for:

- fusing
- reverse polarity
- transient/load-dump tolerance
- grounding between scope, serial, CAN, USB, Ethernet, and vehicle/equipment power
- labeled fixed-role ports rather than ambiguous switching

Dedicated serial ports remain preferable to a general-purpose multiplexer until electrical standards and use cases are stable.

## Source/runtime topology

The public Git repository is the governed source. The appliance under `/opt/remote-dan-lite` is a deployed runtime and is not itself a Git checkout.

Publication and deployment are separate operations:

- a GitHub push updates governed public source
- a deployment copies an approved source revision into the appliance runtime and may restart the service

Do not infer that a feature is live because it appears in Git. Conversely, runtime evidence should be reconciled into governed source rather than becoming permanent hand-edited drift.

## Current next slices

1. Reconcile and deploy the database-backed tabbed source revision to the appliance.
2. Add asset/case/session API surfaces and selectors.
3. Commission the raw Serial acquisition lane.
4. Integrate the Anybus satellite read-only with durable transaction logging.
5. Add richer cross-source timeline correlation.
6. Commission guided test workflows without duplicating acquisition engines.
7. Commission independent recovery hardware and the final enclosure.
