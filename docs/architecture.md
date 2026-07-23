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
- CAN Decode v1 is **private field-fixture proven and in governed source**. It derives generic Classical CAN frame and identifier evidence from existing CAN or eligible Bus Sniffer waveforms without reacquisition or transmission; live deployment remains a separate acceptance gate.
- CSV, JSON, PNG, PDF, and checksum artifacts are **proven**.
- SQLite metadata and evidence lineage are **proven live**.
- Seven core session-centered tabs are **proven live**. Bus Sniffer, OBD, and Modbus expand the governed source to ten tabs and are **simulator-proven**; their first appliance acceptance runs remain commissioning boundaries. The direct OBDLink SX read-only hardware path is separately **proven** against a real vehicle. The Serial receive lane is **in governed source and simulator-proven**; its first real C662 capture remains a commissioning boundary.
- Profile-driven Scope acquisition is **hardware-preflight proven** on the real 2406B, including a 10-second capture and model-accepted ranges from ±20 mV through ±20 V.
- The Anybus AB7702 is a **connected satellite**. Bounded read-only HICP/43-14 discovery and durable transaction evidence are **in governed source and simulator-proven**; real network acceptance remains pending.
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
2. **Bus Sniffer** — passive multi-window physical-layer classification and compatible-lane recommendation
3. **Scope** — profile-driven physical-signal acquisition with four configurable channels
4. **Serial** — raw serial configuration, capture, and decode evidence
5. **CAN** — listen-only acquisition, battery gauge, CAN-H/CAN-L evidence, passive signal intelligence, confidence-ranked protocol fingerprints, and generic decode of existing eligible captures
6. **OBD** — active generic SAE J1979 live data, readiness, DTC, VIN, records, and evidence through a direct USB OBDLink SX provider
7. **Modbus** — manually initiated, interface-scoped HICP and Modbus 43/14 identity discovery
8. **Tests** — guided workflows that configure the shared acquisition engines
9. **Timeline** — correlated scope, CAN, serial, OBD, test, and operator events
10. **Evidence** — session packages, lineage, raw artifacts, calculations, reports, and operator findings

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

### CAN Decode v1 boundary

CAN Decode v1 is a derived-evidence workflow over an existing immutable waveform. It does not trigger Pico acquisition. The decoder tries the recorded CAN-H/CAN-L orientation first and retries with the logical pair reversed only when the expected orientation produces no complete valid frames. Raw channel labels and source bytes are retained unchanged; reversed polarity is persisted as an explicit warning.

Published frame rows require complete Classical CAN structure: valid stuffing through the CRC sequence, CRC-15, recessive CRC delimiter, recessive ACK delimiter, seven recessive EOF bits, and complete source-sample bounds. The ACK slot itself may be dominant or recessive. CRC-only or truncated candidates remain rejected and never expose payload as trusted evidence. CAN FD candidates are counted as unsupported rather than decoded as Classical CAN.

Each explicit decode action creates a child evidence run containing `frames.jsonl`, `identifiers.csv`, `summary.json`, and `manifest.json`. The child stores the source/parent run ID, authoritative SQLite parent capture ID, source filename and SHA-256, decoder settings, polarity, nominal bitrate, frame/identifier/rejected counts, inherited session, limitations, and `writes_performed: 0`. It never mutates the source manifest, waveform, SQLite capture row, or source artifact records.

The current UI and API are intentionally generic. They show frame timestamps, standard/extended identifiers, RTR/DLC, payload bytes, cadence, payload transitions, and byte-change counts. They do not provide DBC decoding, OEM signal meaning, PID/VIN extraction, ISO-TP/UDS reassembly, CAN FD payload decoding, transmit, ACK generation, replay, stimulation, or queries.

### Active generic OBD boundary

The OBD lane is intentionally separate from passive CAN acquisition. It actively sends bounded SAE J1979 requests through one long-lived, serialized provider while preserving the source ECU and exact adapter transcript. The first hardware provider opens only the configured `/dev/serial/by-id/...` OBDLink SX path at 115200 baud, requests exclusive tty ownership, holds a process-external lock, and is deliberately pinned to the commissioned ISO 15765-4 CAN 11/500 transport. Other J1979 transports are not yet claimed. It does not silently fall back from hardware to simulator.

Supported-PID discovery starts at Mode 01 PID 00 and follows continuation pages only when advertised. Initial normalized reads cover readiness/MIL, load, temperatures, fuel trims, MAP, RPM, speed, timing, MAF, throttle, runtime, fuel level, and module voltage. DTC reads retain stored, pending, and permanent classifications separately. Mode 09 VIN observations retain the reporting ECU and surface mismatches rather than choosing silently.

This is generic emissions/powertrain diagnostics, not complete manufacturer-enhanced coverage. ABS, SRS, EyeSight, body, coding, actuator tests, security access, reflashing, and programming are outside this provider.

Mode 04 is consequential: it can clear stored emissions DTCs, freeze-frame/test information, and readiness completion, while permanent DTCs may remain. The current UI and API fail closed because the service has no authenticated operator principal. A typed phrase or LAN allowlist alone is not enough. Hardware clear requires a later authenticated prepare/commit flow with pre-clear evidence, stationary/RPM and voltage checks, a short-lived connection-bound token, one command attempt with no automatic retry, post-clear evidence, and an append-only audit record even when the result is ambiguous.

### Passive Serial boundary

The Serial lane preserves raw receive bytes, timestamped userspace read chunks, decoded text/hex, receiver error counters, a timing plot, report, checksum manifest, and SQLite capture/artifact lineage. Protocol fingerprints are fail-closed:

- SEL ASCII requires prompt grammar plus structured identity fields for high confidence;
- SEL Fast Message requires `A5 46`, exact length, recognized function, and CRC-16;
- Modbus RTU requires legal address/function/length and CRC-16, while high confidence also requires trustworthy frame boundaries and multiple nonidentical frames;
- DNP3 requires link length/control semantics plus header and payload-block CRCs;
- IEC 60870-5-101 requires complete fixed/variable frame structure and checksum;
- one checksum-valid frame remains a candidate, and silence does not infer baud, parity, or protocol.

The Linux implementation is application receive-only, not electrically isolated. `O_RDONLY` blocks application writes on that descriptor, but CP210x TXD remains an output and DTR/RTS can transition during open, configure, enumeration, reset, close, or unplug. Production passive wiring therefore connects only RXD and a verified-safe reference; TXD, DTR, RTS, and all other outputs remain physically disconnected and insulated. Userspace USB read timestamps are retained honestly as chunk timing and are not represented as per-character edge timing.

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
                 ├── test result
                 └── OBD snapshot
                      ├── DTC observation
                      └── live-value observation
```

SQLite schema version 2 migrates in place, preserves schema-v1 evidence, normalizes customers while retaining legacy free-form customer names, reuses `assets` for vehicles, and adds OBD connection/snapshot/value/DTC lineage plus database-enforced append-only clear audit events. It does not store large waveform or report files as database blobs.

Derived evidence preserves the same capture/artifact model while carrying explicit parent identifiers and source hashes in the child manifest, summary, and SQLite metadata. Parent identity is resolved by exact source `run_id`; a manifest-provided capture ID is not treated as authoritative.

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

Implemented discovery is read-only: one interface-selected HICP broadcast plus one Modbus 43/14 Read Device Identification request per remaining bounded host, with no retry, register read, or write fallback. HICP identity is retained as an unauthenticated observation rather than authentication. Foreign/conflicting addresses are not followed.

Future structured register transactions remain a separate integration slice and must begin read-only.

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

1. Deploy and accept the nine-tab source revision on the appliance.
2. Commission the raw Serial acquisition lane with the C662.
3. Run the first governed on-network Anybus/HICP/43-14 discovery and verify gateway service continuity.
4. Commission protected Bus Sniffer inputs and the first real Pico survey without adding termination or drive.
5. Add asset/case/session API surfaces and selectors.
6. Add richer cross-source timeline correlation.
7. Commission guided test workflows and independent recovery hardware without duplicating acquisition engines.
