# Independent Verification Report: SpaceWire Light `spwstream`

| | |
|---|---|
| **DUT** | SpaceWire Light v20130504, `spwstream` (FIFO interface), generic RX/TX implementations. Source: freecores mirror, commit `d26037f`. |
| **Reference** | ECSS-E-ST-50-12C (SpaceWire), SpaceWire Light Manual v20130504 |
| **Method** | Black-box cocotb 2.1 testbench on GHDL 7 with a Python SpaceWire link partner; formal verification of the exchange-level controller `spwlink` with SymbiYosys (PSL, GHDL front-end) |
| **Configuration** | sysclk 20 MHz (also 40 and 100 MHz for timing), 10 Mbit/s link, RX/TX FIFO 64 bytes; `impl_fast` clock-domain-crossing variants and the AMBA wrapper are out of scope |
| **Author / date** | Talha Demir, 22 September 2026 |

## 1. Summary

The core is solid. Link initialisation, flow control, error recovery, time-codes and packet integrity behave as the standard requires, including under randomised bidirectional traffic with application stalls. The vendor's own 23-configuration testbench passes on GHDL 7 (baseline). Independent testing found **one ECSS timing non-compliance**, **one protocol-level hazard**, **two error-reporting inconsistencies** and three documentation gaps. One suspected flow-control bug found by code review was **disproved** by a directed phase-sweep test and by formal proof.

| Result | Count |
|---|---|
| cocotb tests written | 17 (13 directed, 1 phase-sweep, 1 randomised soak, 2 characterisation) |
| cocotb tests passing | 16; the failing test is the ECSS timing check (finding F1) |
| Formal properties on `spwlink` | 42 assertions, 12 environment assumptions, 6 cover points |
| Formal result | PDR (IC3, ABC) proves all 42 assertions unboundedly in 3 s under the 12 interface assumptions; all 6 cover points reached (BMC depth 40); k-induction not needed |
| Simulation throughput | 3.9 ms of link time in 9 s wall clock |

## 2. Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | **Medium** (ECSS non-compliance) | Disconnect timeout exceeds the ECSS maximum at the manual's recommended clock. `disconnect_time = sysfreq x 850 ns` ignores the receiver pipeline (2 synchroniser flops, decoder, link FSM, ~4.5 clocks). Measured from the last D/S transition to `errdisc`: **1093 ns at 20 MHz** (limit 727..1000 ns), 968 ns at 40 MHz, 893 ns at 100 MHz. Compliance needs sysclk above ~34 MHz, while manual 8.2 recommends "at least 20 MHz" and the vendor testbench uses 20 MHz. Fix: subtract the pipeline depth from `disconnect_time`, or document the minimum clock. | `test_06b`, ECSS 8.9.2.1 |
| F2 | **Medium** (protocol hazard) | A `linkdis` pulse after a link error cancels TX discarding. If software reacts to an error by pulsing `linkdis` (a common recovery step), the remaining 31 of 40 bytes of the aborted packet were transmitted as a new packet once the link returned, with no EEP and no delimiter. The receiver cannot tell it holds a fragment. Manual 4.2 documents the mechanism; the interaction with a preceding error is not covered. Fix: clear `txdiscard` on `linkdis` only if the link was running when `linkdis` was asserted, or never clear it before the next EOP/EEP. | `test_12` |
| F3 | Low (error reporting) | `errcred` is raised for an N-char received before the link is in Run (ErrorWait, Ready, Started, Connecting). ECSS treats this as a silent reset; the sibling outputs `errdisc`, `errpar`, `erresc` are gated to Run. Application error counters will attribute a restart glitch to a flow-control failure. Found while writing the formal environment; confirmed in simulation. Fix: gate `linko.errcred` like the other error outputs. | `test_16`, formal harness |
| F4 | Low (interface consistency) | `errcred` stays high for 2 clocks; the other three error outputs are 1-clock pulses. Pulse counters double-count. | `test_09` |
| F5 | Info (documentation) | A `linkdis` pulse during Started or Connecting is ignored and the link proceeds to Run. This matches the ECSS state diagram, but not the manual's "do not start link". | `test_13`, ECSS 8.5.2 |
| F6 | Info (documentation) | `tick_in` issued before Run is dropped silently; manual 4.2 says time-codes are "buffered until they can be transmitted". | `test_10` |
| F7 | Info (undocumented behaviour) | A character is delivered only after the *next* character's parity bit verified it. Consequences: the character protected by a failing parity bit is dropped (good), the last character before a disconnect is never delivered (harmless, packet is EEP-terminated), and an N-char received without credit is still delivered before the EEP. | `test_06`, `test_07`, `test_09` |
| H0 | Disproved | Code review suggested the FCT decision in `spwlink` compared new credit with one-cycle-stale `rxroom` (one-byte over-commit, silent loss when the RX FIFO is full). A 16-phase sweep aligning character arrival with the transmitter's FCT slot never over-committed, and the formal properties "credit never exceeds room" and "FCT granted only if credit+8 fits room" hold. The decision uses the pre-character values consistently. | `test_05`, formal |

## 3. ECSS timing measurements (20 MHz)

| Requirement | ECSS value | Measured |
|---|---|---|
| ErrorReset + ErrorWait before Started | 6.4 + 12.8 us | 19.35 us |
| Started / Connecting timeout | 12.8 us | 12.90 us |
| Initial signalling rate | 10 Mbit/s | 100.0 ns per bit, 0 violations over all tests |
| Disconnect timeout | 850 ns (727..1000) | 1093 ns (F1) |
| Maximum credit outstanding | 56 | 56 (7 FCT), enforced both directions |

## 4. What was verified

**Simulation (cocotb, black-box).** The Python link partner encodes and decodes Data/Strobe at the bit level, checks parity and bit period on everything the DUT transmits, tracks credit in both directions and can inject bad parity, illegal escape sequences, credit violations, disconnects and phase shifts. Tests cover: reset state; link start with and without a partner, autostart, timeouts and retry; NULL/FCT ordering and initial credit; packets with EOP/EEP both ways; RX back-pressure with FIFO full; disconnect, parity, escape and credit errors with EEP injection and relink; time-codes both ways, priority over data, non-consecutive values; TX discard after a link error; `linkdis` interactions; TX FIFO full and mid-traffic reset; 40 random packets per direction with random application stalls and TX rate changes. All checks are scoreboarded; the credit-versus-room invariant is checked at every FCT.

**Formal (SymbiYosys, PSL on `spwlink`).** The receiver, transmitter and application are free inputs constrained only by the contracts in `spwpkg.vhd` (no tokens before the first NULL, sticky `gotnull`, acks only for requests and only while the transmitter is enabled, `rxroom` may drop by one only after `rxchar`). Properties: state exclusivity and legal transitions (Run only from Connecting, Connecting only from Started, Started needs room for one FCT and no disable), transmitter enables per state, every error or disable leaves Run in one clock, ErrorReset dwell of exactly `reset_time+1` clocks, Started/Connecting timeout bound, N-chars and time-codes delivered only in Run, credit granted never above 56 nor above RX room, FCT granted only if credit+8 fits, credit received never above 56 unless `errcred`, N-chars handed to the transmitter only with credit, `errcred` raised exactly on overrun or over-grant. Cover points reach Run, FCT and data acks, credit error, disconnect in Run and the Started timeout.

## 5. Out of scope / next steps

`impl_fast` receiver and transmitter (separate clock domains) were not tested; a CDC review and a multi-clock cocotb configuration are the natural next step. Code coverage was not collected (this GHDL build has no coverage support); functional coverage is by test intent. The AMBA/GRLIB wrapper was not built.

## 6. Reproduce

```
./setup.sh && source ./env.sh
make -C tb                       # 17 tests, ~10 s
make -C tb SYSFREQ_KHZ=40000 SIM_BUILD=sim_build_40 COCOTB_TESTCASE=test_06b_disconnect_timeout_ecss_window
cd formal && sby -f spwlink.sby pdr cover     # unbounded proof + reachability, seconds
```
