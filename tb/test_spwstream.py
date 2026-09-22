"""
cocotb tests for SpaceWire Light `spwstream` (black-box, via the SpaceWire
Data/Strobe pins and the application FIFO interface).

Generics are fixed by the Makefile: sysfreq=20 MHz (50 ns clock), generic
RX/TX implementations, 10 Mbit/s link during handshake (divcnt=1 -> 100 ns bit).
"""
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, ClockCycles, Timer
from cocotb.utils import get_sim_time

from spw_bfm import SpwLinkPartner, NULL, FCT, DATA, EOP, EEP, TIMECODE

import os
SYSFREQ_KHZ = int(os.environ.get("SYSFREQ_KHZ", "20000"))
RXFIFO_BITS = int(os.environ.get("RXFIFO_BITS", "6"))
CLK_NS = 1.0e6 / SYSFREQ_KHZ
RX_CAPACITY = 2 ** RXFIFO_BITS - 1
BIT_NS = 100.0          # 10 Mbit/s: ECSS initial rate
US = 1000.0


# ----------------------------------------------------------------------------
# Application-side helpers (the "user" of the FIFO interface)
# ----------------------------------------------------------------------------
def now():
    return get_sim_time(unit="ns")


async def reset_dut(dut, cycles=5):
    for name in ("rst", "autostart", "linkstart", "linkdis", "tick_in",
                 "txwrite", "txflag", "rxread"):
        getattr(dut, name).value = 0
    dut.txdivcnt.value = 1
    dut.ctrl_in.value = 0
    dut.time_in.value = 0
    dut.txdata.value = 0
    dut.rxclk.value = 0
    dut.txclk.value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, cycles)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


class App:
    """Drives txwrite/txdata and rxread with the documented handshakes."""

    def __init__(self, dut):
        self.dut = dut
        self.rx = []            # ('D', byte) | ('EOP',) | ('EEP',)
        self.timecodes = []
        self._reader = None
        self._tc_mon = None

    async def write(self, chars, timeout_ns=2_000_000.0):
        """chars: iterable of int (data byte) or 'EOP' / 'EEP'.
        Inputs are driven on the falling edge, the handshake is sampled on
        the rising edge (txwrite & txrdy both high => character stored)."""
        d = self.dut
        t0 = now()
        for ch in chars:
            await FallingEdge(d.clk)
            if ch == "EOP":
                d.txflag.value, d.txdata.value = 1, 0
            elif ch == "EEP":
                d.txflag.value, d.txdata.value = 1, 1
            else:
                d.txflag.value, d.txdata.value = 0, int(ch)
            d.txwrite.value = 1
            while True:
                await RisingEdge(d.clk)
                if int(d.txrdy.value) == 1:
                    break
                if now() - t0 > timeout_ns:
                    raise TimeoutError("txrdy stayed low")
        await FallingEdge(d.clk)
        d.txwrite.value = 0

    def start_reader(self):
        self._reader = cocotb.start_soon(self._read_loop())

    def stop_reader(self):
        if self._reader:
            self._reader.cancel()
            self._reader = None
        self.dut.rxread.value = 0

    async def _read_loop(self):
        d = self.dut
        await FallingEdge(d.clk)
        d.rxread.value = 1
        while True:
            await RisingEdge(d.clk)
            if int(d.rxvalid.value) == 1:
                if int(d.rxflag.value) == 1:
                    self.rx.append(("EEP",) if int(d.rxdata.value) == 1 else ("EOP",))
                else:
                    self.rx.append(("D", int(d.rxdata.value)))

    async def read_n(self, n, timeout_ns=1_000_000.0):
        """Read exactly n characters, then deassert rxread (falling edge)."""
        d = self.dut
        got = []
        t0 = now()
        await FallingEdge(d.clk)
        d.rxread.value = 1
        while len(got) < n:
            await RisingEdge(d.clk)
            if int(d.rxvalid.value) == 1:
                if int(d.rxflag.value) == 1:
                    got.append(("EEP",) if int(d.rxdata.value) == 1 else ("EOP",))
                else:
                    got.append(("D", int(d.rxdata.value)))
            if now() - t0 > timeout_ns:
                await FallingEdge(d.clk)
                d.rxread.value = 0
                raise TimeoutError(f"read {len(got)} of {n} chars")
        await FallingEdge(d.clk)
        d.rxread.value = 0
        self.rx.extend(got)
        return got

    async def wait_rx(self, n, timeout_ns=1_000_000.0):
        t0 = now()
        while len(self.rx) < n:
            await ClockCycles(self.dut.clk, 20)
            if now() - t0 > timeout_ns:
                raise TimeoutError(f"app received {len(self.rx)} of {n} chars")
        return self.rx[:n]

    def start_tc_monitor(self):
        self._tc_mon = cocotb.start_soon(self._tc_loop())

    async def _tc_loop(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            if int(d.tick_out.value) == 1:
                self.timecodes.append((int(d.ctrl_out.value), int(d.time_out.value), now()))

    async def send_timecode(self, ctrl, time):
        d = self.dut
        await FallingEdge(d.clk)
        d.ctrl_in.value = ctrl
        d.time_in.value = time
        d.tick_in.value = 1
        await FallingEdge(d.clk)
        d.tick_in.value = 0


class ErrMon:
    """Records the sim time of every 1-cycle pulse on the error outputs."""
    NAMES = ("errdisc", "errpar", "erresc", "errcred")

    def __init__(self, dut):
        self.dut = dut
        self.pulses = {n: [] for n in self.NAMES}
        self._task = cocotb.start_soon(self._loop())

    async def _loop(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            for n in self.NAMES:
                if int(getattr(d, n).value) == 1:
                    self.pulses[n].append(now())

    async def wait(self, name, timeout_ns, since_ns=0.0):
        t0 = now()
        while True:
            for t in self.pulses[name]:
                if t >= since_ns:
                    return t
            if now() - t0 > timeout_ns:
                raise TimeoutError(f"no {name} pulse within {timeout_ns} ns")
            await ClockCycles(self.dut.clk, 2)

    def count(self, name):
        return len(self.pulses[name])

    def events(self, name):
        """Group consecutive-cycle samples into (t_start, width_cycles)."""
        ev = []
        for t in self.pulses[name]:
            if ev and abs(t - (ev[-1][0] + ev[-1][1] * CLK_NS)) < 1.0:
                ev[-1] = (ev[-1][0], ev[-1][1] + 1)
            else:
                ev.append((t, 1))
        return ev


async def wait_signal(sig, value, clk, timeout_ns, what=""):
    t0 = now()
    while int(sig.value) != value:
        await RisingEdge(clk)
        if now() - t0 > timeout_ns:
            raise TimeoutError(f"{what or sig._name} did not become {value} within {timeout_ns} ns")
    return now()


async def bring_link_up(dut, partner, timeout_ns=80 * US):
    """linkstart=1 with an active partner; returns time `running` rose."""
    partner.idle_nulls = True
    dut.linkstart.value = 1
    return await wait_signal(dut.running, 1, dut.clk, timeout_ns, "running")


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    await reset_dut(dut)
    partner = SpwLinkPartner(dut, BIT_NS)
    partner.expected_bit_ns = BIT_NS
    partner.start()
    return partner, App(dut)


def packet(n, seed):
    rng = random.Random(seed)
    return [rng.randrange(256) for _ in range(n)]


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
@cocotb.test()
async def test_01_reset_state(dut):
    """After reset: link idle, D/S low, TX FIFO ready, RX FIFO empty."""
    partner, app = await setup(dut)
    await ClockCycles(dut.clk, 4)
    for name in ("started", "connecting", "running", "errdisc", "errpar",
                 "erresc", "errcred", "rxvalid", "tick_out", "spw_do", "spw_so"):
        assert int(getattr(dut, name).value) == 0, f"{name} not 0 after reset"
    assert int(dut.txrdy.value) == 1, "txrdy should be 1 after reset"
    # link must not start by itself
    await Timer(40 * US, unit="ns")
    assert int(dut.started.value) == 0 and int(dut.running.value) == 0
    assert partner.nulls == 0, "DUT transmitted although link was never started"


@cocotb.test()
async def test_02_link_init_timing(dut):
    """ErrorReset(6.4us)+ErrorWait(12.8us) before Started; NULLs at 10 Mbit;
    Started timeout (12.8us) when the partner stays silent; retry cycle."""
    partner, app = await setup(dut)
    t_rst = now()
    dut.linkstart.value = 1
    t_started = await wait_signal(dut.started, 1, dut.clk, 40 * US, "started")
    dt = t_started - t_rst
    dut._log.info(f"reset -> Started after {dt/1000:.2f} us (expect ~19.2 us)")
    assert 18.5 * US < dt < 20.5 * US, f"Started after {dt} ns"
    tok = await partner.wait_token(NULL, 5 * US)
    assert tok is partner.tokens[0], f"first token from DUT is {partner.tokens[0]}, expected NULL"
    t_fall = await wait_signal(dut.started, 0, dut.clk, 20 * US, "started (timeout)")
    dt2 = t_fall - t_started
    dut._log.info(f"Started timeout after {dt2/1000:.2f} us (expect ~12.8 us)")
    assert 12.0 * US < dt2 < 13.6 * US, f"Started timeout after {dt2} ns"
    assert int(dut.connecting.value) == 0 and int(dut.running.value) == 0
    assert int(dut.errdisc.value) == 0, "timeout must not be reported as errdisc"
    # only NULLs were sent, at exactly 100 ns/bit
    kinds = {t.kind for t in partner.tokens}
    assert kinds == {NULL}, f"unexpected tokens in Started: {kinds}"
    assert partner.bit_period_errors == 0, f"{partner.bit_period_errors} bit-period violations"
    # retry: Started again after another ErrorReset+ErrorWait
    t_again = await wait_signal(dut.started, 1, dut.clk, 40 * US, "started (retry)")
    dt3 = t_again - t_fall
    assert 18.5 * US < dt3 < 20.5 * US, f"retry after {dt3} ns"
    partner.stop()


@cocotb.test()
async def test_03_link_up_and_initial_credit(dut):
    """Full handshake with an active partner; DUT sends NULL before FCT;
    initial credit granted to partner is 7 FCT = 56 chars (RX FIFO 63 free)."""
    partner, app = await setup(dut)
    t_run = await bring_link_up(dut, partner)
    dut._log.info(f"running at {t_run/1000:.2f} us")
    kinds = [t.kind for t in partner.tokens]
    assert kinds[0] == NULL, f"first token {kinds[0]}"
    assert FCT in kinds, "DUT never sent FCT"
    assert kinds.index(NULL) < kinds.index(FCT), "FCT before NULL"
    await Timer(8 * US, unit="ns")
    assert partner.fcts == 7, f"DUT granted {partner.fcts} FCTs, expected 7 (56 credit)"
    assert partner.tx_credit == 56
    assert partner.parity_errors == 0 and partner.esc_errors == 0
    assert partner.bit_period_errors == 0
    assert partner.link_resets == 0
    assert int(dut.running.value) == 1
    partner.stop()


@cocotb.test()
async def test_04_data_both_directions(dut):
    """Packets in both directions with EOP/EEP, in order, no loss."""
    partner, app = await setup(dut)
    await bring_link_up(dut, partner)
    app.start_reader()
    # partner -> DUT
    pk1, pk2 = packet(20, 1), packet(40, 2)
    partner.send_packet(pk1, "eop")
    partner.send_packet(pk2, "eep")
    exp_rx = [("D", b) for b in pk1] + [("EOP",)] + [("D", b) for b in pk2] + [("EEP",)]
    # DUT -> partner
    pk3, pk4 = packet(30, 3), packet(10, 4)
    await app.write(pk3 + ["EOP"] + pk4 + ["EEP"])
    exp_tx = [("D", b) for b in pk3] + [("EOP",)] + [("D", b) for b in pk4] + [("EEP",)]
    got_rx = await app.wait_rx(len(exp_rx), 500 * US)
    got_tx = await partner.wait_rx_chars(len(exp_tx), 500 * US)
    assert got_rx == exp_rx, f"RX mismatch:\n got {got_rx}\n exp {exp_rx}"
    assert got_tx == exp_tx, f"TX mismatch:\n got {got_tx}\n exp {exp_tx}"
    await Timer(5 * US, unit="ns")
    assert len(app.rx) == len(exp_rx), "extra chars received"
    assert partner.dut_credit_violations == 0, "DUT sent beyond granted credit"
    assert partner.parity_errors == 0 and partner.esc_errors == 0
    assert partner.link_resets == 0 and int(dut.running.value) == 1
    partner.stop()


@cocotb.test()
async def test_05_credit_never_exceeds_rx_room(dut):
    """Invariant: total credit granted by the DUT (FCTs x 8) must never exceed
    the RX FIFO room (63 usable bytes) plus bytes already read by the app.
    The application never reads during the burst, so the DUT must stop at
    7 FCTs (56 bytes). The partner's character timing is shifted by k clock
    cycles (k = 0..15) against the DUT's transmitter, because the FCT
    decision in spwlink is only sampled by the transmitter once per NULL
    (16 clocks) and a 1-cycle window would otherwise be missed."""
    partner, app = await setup(dut)
    capacity = RX_CAPACITY
    over = {}
    for k in range(16):
        await reset_dut(dut)
        partner.clear_stats()
        await bring_link_up(dut, partner)
        await Timer(8 * US, unit="ns")
        assert partner.fcts == 7 and partner.tx_credit == 56, (partner.fcts, partner.tx_credit)
        # phase shift of k clocks: pauses < 850 ns, split around a NULL
        k1, k2 = min(k, 8), max(k - 8, 0)
        partner.send(("pause", k1 * CLK_NS), ("null",), ("pause", k2 * CLK_NS))
        partner.send_packet(list(range(56)), end=None)
        await partner.flush()
        await Timer(3 * US, unit="ns")
        granted = partner.fcts * 8
        dut._log.info(f"k={k:2d}: after 56-byte burst, app idle: FCTs={partner.fcts} "
                      f"credit granted={granted} (room was {capacity})")
        if granted > capacity:
            # Demonstrate the consequence: partner uses the extra credit.
            extra = list(range(100, 100 + partner.tx_credit))
            partner.send_packet(extra, end=None)
            await partner.flush()
            await Timer(3 * US, unit="ns")
            got = await app.read_n(capacity, 300 * US)
            await Timer(2 * US, unit="ns")
            more = []
            if int(dut.rxvalid.value):
                more = await app.read_n(1, 5 * US)
            exp = [("D", b) for b in list(range(56)) + extra]
            over[k] = dict(fcts=partner.fcts, sent=partner.chars_sent,
                           received=len(got) + len(more),
                           missing=[e for e in exp if e not in got + more],
                           errcred=int(dut.errcred.value), running=int(dut.running.value),
                           resets=partner.link_resets)
            dut._log.info(f"k={k:2d}: OVER-COMMIT -> sent {over[k]['sent']} bytes, "
                          f"app read back {over[k]['received']}, missing {over[k]['missing']}")
    partner.stop()
    assert not over, (f"DUT granted more credit than RX FIFO room in {len(over)}/16 phases: "
                      + "; ".join(f"k={k}: {v}" for k, v in over.items()))


# ----------------------------------------------------------------------------
# Error injection / ECSS exchange-level behaviour
# ----------------------------------------------------------------------------
async def wait_pulse(sig, clk, timeout_ns, what=""):
    return await wait_signal(sig, 1, clk, timeout_ns, what)


async def relink(dut, partner, timeout_ns=80 * US):
    """After a link error: wait for running=0, then for the link to come back."""
    await wait_signal(dut.running, 0, dut.clk, 10 * US, "running -> 0")
    return await wait_signal(dut.running, 1, dut.clk, timeout_ns, "running -> 1 (relink)")


@cocotb.test()
async def test_06_disconnect_in_run(dut):
    """Partner goes silent mid-packet: errdisc within ECSS window (727..1000 ns
    after the last transition), link resets, EEP appended to the partial
    packet, no further data, link re-establishes when the partner returns."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    app.start_reader()
    pk = packet(10, 6)
    partner.send_packet(pk, end=None)
    partner.send(("pause", 3000.0))       # silence: disconnect
    await app.wait_rx(10, 100 * US)
    t_err = await err.wait("errdisc", 10 * US)
    dt = t_err - partner.last_tx_transition_ns
    dut._log.info(f"errdisc {dt:.0f} ns after last transition (ECSS: 727..1000 ns)")
    await Timer(2 * US, unit="ns")
    assert err.count("errdisc") == 1, f"errdisc pulses: {err.pulses['errdisc']} (must be a single 1-cycle pulse)"
    assert err.count("errpar") == err.count("erresc") == err.count("errcred") == 0
    # A character is delivered only once the *next* character's parity bit has
    # verified it. The 10th byte was followed by silence, so it is never
    # delivered: 9 bytes + EEP is the correct outcome.
    assert app.rx == [("D", b) for b in pk[:9]] + [("EEP",)], f"got {app.rx}"
    assert int(dut.running.value) == 0
    await relink(dut, partner)
    await Timer(2 * US, unit="ns")
    assert app.rx[9:] == [("EEP",)], "no extra chars expected after relink"
    # new packet after relink must arrive intact
    pk2 = packet(5, 7)
    partner.send_packet(pk2, "eop")
    await app.wait_rx(10 + 6, 100 * US)
    assert app.rx[10:] == [("D", b) for b in pk2] + [("EOP",)]
    partner.stop()
    partner.stop()


@cocotb.test()
async def test_06b_disconnect_timeout_ecss_window(dut):
    """ECSS-E-ST-50-12C 8.9.2.1: disconnect shall be detected 850 ns nominal
    (727 ns min, 1000 ns max) after the last transition. Measured from the
    last D/S transition driven by the partner to the errdisc pulse."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    app.start_reader()
    partner.send_packet(packet(4, 66), end=None)
    partner.send(("pause", 3000.0))
    t_err = await err.wait("errdisc", 50 * US)
    dt = t_err - partner.last_tx_transition_ns
    dut._log.info(f"sysclk {SYSFREQ_KHZ/1000:.0f} MHz: errdisc {dt:.0f} ns after the last transition "
                  f"(disconnect_time generic = {int(SYSFREQ_KHZ*1e3*850e-9)} clocks = "
                  f"{int(SYSFREQ_KHZ*1e3*850e-9)*CLK_NS:.0f} ns, plus input pipeline)")
    partner.stop()
    assert 727.0 <= dt <= 1000.0, (
        f"FINDING: disconnect detected {dt:.0f} ns after the last transition at "
        f"{SYSFREQ_KHZ/1000:.0f} MHz system clock; ECSS window is 727..1000 ns. "
        f"disconnect_time = sysfreq*850ns does not account for the receiver pipeline "
        f"(2 synchroniser flops + decode + link FSM).")


@cocotb.test()
async def test_07_parity_error(dut):
    """Data char with wrong parity in Run: errpar pulse, link reset, EEP
    appended (packet in progress), link recovers, later traffic intact."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    app.start_reader()
    pk = packet(6, 8)
    partner.send_packet(pk, end=None)
    partner.send(("data", 0x5A, True))    # parity bit flipped (covers pk[5])
    partner.send(("data", 0x11,))         # partner keeps going (should be ignored)
    await err.wait("errpar", 50 * US)
    await wait_signal(dut.running, 0, dut.clk, 5 * US, "running")
    await Timer(2 * US, unit="ns")
    assert err.count("errpar") == 1 and err.count("errdisc") == 0
    # The parity bit of character k+1 protects the data bits of character k.
    # spwstream delivers character k only after that check, so pk[5] is
    # dropped and the packet is terminated with EEP.
    assert app.rx == [("D", b) for b in pk[:5]] + [("EEP",)], f"got {app.rx}"
    dut._log.info("parity error: char protected by the failing parity bit was dropped, then EEP")
    n_before = len(app.rx)
    await relink(dut, partner)
    pk2 = packet(4, 9)
    partner.send_packet(pk2, "eop")
    await app.wait_rx(n_before + 5, 100 * US)
    assert app.rx[n_before:] == [("D", b) for b in pk2] + [("EOP",)]
    partner.stop()


@cocotb.test()
async def test_08_escape_errors(dut):
    """ESC+EOP, ESC+EEP, ESC+ESC are escape errors: erresc, link reset, recovery."""
    partner, app = await setup(dut)
    await bring_link_up(dut, partner)
    app.start_reader()
    for seq in (("esc", "eop"), ("esc", "eep"), ("esc", "esc")):
        n0 = len(app.rx)
        partner.send_packet(packet(3, 10), end=None)
        partner.enforce_credit = False
        partner.send(("esc",), (seq[1],))
        partner.enforce_credit = True
        await wait_pulse(dut.erresc, dut.clk, 50 * US, f"erresc for {seq}")
        await ClockCycles(dut.clk, 3)
        assert int(dut.erresc.value) == 0
        await Timer(2 * US, unit="ns")
        assert app.rx[-1] == ("EEP",), f"{seq}: packet not terminated by EEP: {app.rx[n0:]}"
        await relink(dut, partner)
        await Timer(3 * US, unit="ns")
    assert partner.link_resets == 3
    partner.stop()


@cocotb.test()
async def test_09_credit_errors(dut):
    """(a) N-char without credit -> errcred. (b) 8th FCT (credit > 56) -> errcred."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    # (a) use up all credit (app does not read), then send one more character
    partner.send_packet(list(range(56)), end=None)
    await partner.flush()
    await Timer(3 * US, unit="ns")
    assert partner.tx_credit == 0, partner.tx_credit
    partner.enforce_credit = False
    partner.send(("data", 0xEE))
    t_a = await err.wait("errcred", 50 * US)
    partner.enforce_credit = True
    await wait_signal(dut.running, 0, dut.clk, 5 * US, "running")
    await Timer(2 * US, unit="ns")
    got = await app.read_n(58, 100 * US)          # 56 data + violating char + injected EEP
    # Observed: the N-char received *without* credit is still delivered to the
    # application (there was room), then the packet is closed with EEP.
    assert got == [("D", b) for b in range(56)] + [("D", 0xEE), ("EEP",)], got[-3:]
    assert int(dut.rxvalid.value) == 0
    dut._log.info("credit violation: offending char delivered to app, then EEP, then link reset")
    await relink(dut, partner)
    await Timer(4 * US, unit="ns")
    ev = err.events("errcred")
    assert len(ev) == 1, ev
    if ev[0][1] != 1:
        dut._log.warning(f"FINDING: errcred asserted for {ev[0][1]} consecutive cycles; errdisc/errpar/"
                         f"erresc are 1-cycle pulses. spwpkg says 'auto-clearing' without a width.")
    # (b) too many FCTs: partner has granted 56 outstanding; force one more
    assert partner.outstanding == 56, partner.outstanding
    partner.send(("fct",))
    await err.wait("errcred", 50 * US, since_ns=t_a + 1)
    await relink(dut, partner)
    assert len(err.events("errcred")) == 2, err.events("errcred")
    assert partner.dut_credit_violations == 0
    partner.stop()


@cocotb.test()
async def test_10_timecodes(dut):
    """Time-codes both directions; TC has priority over queued data; tick_out
    for every received TC (documented ECSS deviation); tick_in while not
    running is dropped (undocumented)."""
    partner, app = await setup(dut)
    app.start_tc_monitor()
    # tick_in before the link runs: what happens to it?
    dut.linkstart.value = 1
    partner.idle_nulls = True
    await wait_signal(dut.started, 1, dut.clk, 60 * US, "started")
    await app.send_timecode(2, 17)                 # link in Started/Connecting
    await wait_signal(dut.running, 1, dut.clk, 60 * US, "running")
    await Timer(10 * US, unit="ns")
    early_tc = list(partner.rx_timecodes)
    dut._log.info(f"tick_in issued before Run: partner received {early_tc} (manual says TCs are buffered)")
    # DUT -> partner with data queued: TC must overtake the queued data
    n0 = len(partner.rx_chars)
    await app.write(list(range(30)))
    await app.send_timecode(1, 33)
    await partner.wait_rx_chars(n0 + 30, 200 * US)
    tcs = [t for t in partner.rx_timecodes if t[2] > early_tc[-1][2]] if early_tc else partner.rx_timecodes
    assert len(tcs) == 1 and tcs[0][:2] == (1, 33), f"TCs seen: {partner.rx_timecodes}"
    idx_tc = next(i for i, t in enumerate(partner.tokens) if t.kind == TIMECODE and (t.value & 0x3f) == 33)
    data_after = [t for t in partner.tokens[idx_tc:] if t.kind == DATA]
    assert len(data_after) >= 25, "time-code did not take priority over queued data"
    # partner -> DUT: consecutive and non-consecutive values all produce tick_out
    for ctrl, tm in ((0, 1), (0, 2), (0, 9), (3, 63), (1, 0)):
        partner.send(("tc", ctrl, tm))
    await partner.flush()
    await Timer(3 * US, unit="ns")
    got = [(c, t) for c, t, _ in app.timecodes]
    assert got == [(0, 1), (0, 2), (0, 9), (3, 63), (1, 0)], f"tick_out sequence {got}"
    assert int(dut.ctrl_out.value) == 1 and int(dut.time_out.value) == 0
    partner.stop()


@cocotb.test()
async def test_11_tx_discard_after_link_error(dut):
    """Link error while the DUT is in the middle of sending a packet: the rest
    of that packet (up to EOP) must be discarded, the next packet sent whole."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    partner.max_outstanding = 8            # throttle DUT: 8 chars per FCT
    pk = packet(40, 11)
    await app.write(pk + ["EOP"])
    await partner.wait_rx_chars(8, 100 * US)
    partner.send(("pause", 3000.0))        # disconnect while DUT mid-packet
    await err.wait("errdisc", 50 * US)
    n_at_err = len(partner.rx_chars)
    await relink(dut, partner)
    await Timer(30 * US, unit="ns")
    assert len(partner.rx_chars) == n_at_err, (
        f"DUT sent {len(partner.rx_chars) - n_at_err} chars of the aborted packet after relink")
    pk2 = packet(12, 12)
    await app.write(pk2 + ["EEP"])
    await partner.wait_rx_chars(n_at_err + 13, 200 * US)
    assert partner.rx_chars[n_at_err:] == [("D", b) for b in pk2] + [("EEP",)]
    partner.stop()


@cocotb.test()
async def test_12_linkdis_cancels_discard(dut):
    """Documented behaviour with a sharp edge: a linkdis pulse after a link
    error clears the discard state, so the tail of the aborted packet is sent
    as a new packet when the link comes back."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    partner.max_outstanding = 8
    pk = packet(40, 13)
    await app.write(pk + ["EOP"])
    await partner.wait_rx_chars(8, 100 * US)
    partner.send(("pause", 3000.0))
    await err.wait("errdisc", 50 * US)
    n_at_err = len(partner.rx_chars)
    # software reacts to the error by disabling and re-enabling the link
    await FallingEdge(dut.clk)
    dut.linkdis.value = 1
    await ClockCycles(dut.clk, 4)
    await FallingEdge(dut.clk)
    dut.linkdis.value = 0
    await relink(dut, partner)
    await Timer(60 * US, unit="ns")
    tail = partner.rx_chars[n_at_err:]
    dut._log.info(f"after linkdis pulse + relink, DUT sent {len(tail)} chars: "
                  f"{tail[:4]} ... {tail[-2:]}")
    full = [("D", b) for b in pk] + [("EOP",)]
    is_suffix = len(tail) > 0 and full[len(full) - len(tail):] == tail
    assert is_suffix, f"tail is not a suffix of the aborted packet: {tail[:5]}"
    dut._log.warning(f"FINDING: after a link error followed by a linkdis pulse, the last "
                     f"{len(tail)} chars of the aborted packet were transmitted as a new packet "
                     f"(no EEP, no leading delimiter) once the link came back. Documented in "
                     f"manual 4.2, but it turns a link error into silent packet truncation.")
    partner.stop()


@cocotb.test()
async def test_13_linkdis_pulse_during_handshake(dut):
    """A linkdis pulse while in Started/Connecting: ECSS 8.5.2 only defines
    [Link Disabled] transitions from Ready (do not start) and Run (disconnect).
    Observe what the core does; the manual says 'do not start link'."""
    partner, app = await setup(dut)
    dut.linkstart.value = 1
    await wait_signal(dut.started, 1, dut.clk, 60 * US, "started")
    partner.idle_nulls = True
    await wait_signal(dut.connecting, 1, dut.clk, 60 * US, "connecting")
    await FallingEdge(dut.clk)
    dut.linkdis.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.linkdis.value = 0
    await Timer(5 * US, unit="ns")
    outcome = ("Run" if int(dut.running.value) else
               "Connecting" if int(dut.connecting.value) else
               "Started" if int(dut.started.value) else "reset/idle")
    dut._log.info(f"linkdis pulse in Connecting -> 5 us later the link is in: {outcome}")
    if outcome == "Run":
        dut._log.warning("FINDING: a linkdis pulse during the handshake (Started/Connecting) is "
                         "ignored and the link proceeds to Run. Consistent with the ECSS state "
                         "diagram, but not with the manual text 'Do not start link'.")
    partner.stop()


@cocotb.test()
async def test_14_tx_fifo_full_then_link_then_reset(dut):
    """TX FIFO accepts 63 chars while the link is down (txrdy drops at 63),
    all are delivered in order once the link runs; a reset empties both FIFOs
    and txrdy/rxvalid reflect that within two clocks."""
    partner, app = await setup(dut)
    data = list(range(1, 64))
    await app.write(data)
    await ClockCycles(dut.clk, 2)
    assert int(dut.txrdy.value) == 0, "txrdy should drop with 63 chars queued (64-byte FIFO)"
    assert int(dut.txhalff.value) == 1
    t0 = now()
    wtask = cocotb.start_soon(app.write([0xAA]))     # must block until room appears
    await Timer(3 * US, unit="ns")
    assert not wtask.done(), "write must stall while the TX FIFO is full"
    await bring_link_up(dut, partner)
    got = await partner.wait_rx_chars(64, 500 * US)
    assert got == [("D", b) for b in data] + [("D", 0xAA)]
    assert wtask.done()
    # partner -> DUT some data, then reset in the middle of everything
    partner.send_packet(list(range(20)), end=None)
    await app.write(list(range(30)))
    await Timer(4 * US, unit="ns")
    await reset_dut(dut, cycles=1)
    await ClockCycles(dut.clk, 2)
    assert int(dut.txrdy.value) == 1, "txrdy must be 1 after reset (FIFO cleared)"
    assert int(dut.rxvalid.value) == 0, "rxvalid must be 0 after reset (FIFO cleared)"
    assert int(dut.running.value) == 0 and int(dut.spw_do.value) == 0 and int(dut.spw_so.value) == 0
    partner.stop()


@cocotb.test()
async def test_15_random_traffic_with_stalls(dut):
    """Randomised bidirectional packets, random application stalls on both
    FIFOs, random TX rate changes; scoreboard checks order and completeness;
    credit/room invariant checked at every FCT."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    await bring_link_up(dut, partner)
    rng = random.Random(15)
    read_by_app = [0]

    async def reader():
        d = dut
        while True:
            await FallingEdge(d.clk)
            d.rxread.value = 1 if rng.random() < 0.6 else 0
            await RisingEdge(d.clk)
            if int(d.rxread.value) == 1 and int(d.rxvalid.value) == 1:
                if int(d.rxflag.value) == 1:
                    app.rx.append(("EEP",) if int(d.rxdata.value) == 1 else ("EOP",))
                else:
                    app.rx.append(("D", int(d.rxdata.value)))
                read_by_app[0] += 1

    async def invariant():
        last = partner.fcts
        while True:
            await RisingEdge(dut.clk)
            if partner.fcts != last:
                last = partner.fcts
                assert partner.fcts * 8 <= RX_CAPACITY + read_by_app[0], (
                    f"credit {partner.fcts*8} > room {RX_CAPACITY} + read {read_by_app[0]}")

    async def rate_changer():
        while True:
            await Timer(round(rng.uniform(20, 80) * US), unit="ns")
            await FallingEdge(dut.clk)
            dut.txdivcnt.value = rng.choice([0, 1, 1, 2, 3])

    cocotb.start_soon(reader())
    cocotb.start_soon(invariant())
    cocotb.start_soon(rate_changer())
    partner.expected_bit_ns = None                    # rate changes are expected now

    exp_rx, exp_tx = [], []
    tx_chars = []
    for i in range(40):
        n = rng.randrange(1, 70)
        pk = [rng.randrange(256) for _ in range(n)]
        end = rng.choice(["eop", "eop", "eep"])
        partner.send_packet(pk, end)
        exp_rx += [("D", b) for b in pk] + [(end.upper(),)]
        n2 = rng.randrange(1, 70)
        pk2 = [rng.randrange(256) for _ in range(n2)]
        end2 = rng.choice(["EOP", "EOP", "EEP"])
        tx_chars += pk2 + [end2]
        exp_tx += [("D", b) for b in pk2] + [(end2,)]
        if rng.random() < 0.3:
            await Timer(round(rng.uniform(1, 20) * US), unit="ns")   # app-side TX gaps
        await app.write(pk2 + [end2])
    await partner.flush()
    got_rx = await app.wait_rx(len(exp_rx), 20_000 * US)
    got_tx = await partner.wait_rx_chars(len(exp_tx), 20_000 * US)
    await Timer(10 * US, unit="ns")
    first_bad = next((i for i, (a, b) in enumerate(zip(app.rx, exp_rx)) if a != b), None)
    assert app.rx == exp_rx, f"partner->DUT mismatch at index {first_bad}: got {app.rx[first_bad:first_bad+4] if first_bad is not None else None}"
    first_bad = next((i for i, (a, b) in enumerate(zip(partner.rx_chars, exp_tx)) if a != b), None)
    assert partner.rx_chars == exp_tx, f"DUT->partner mismatch at index {first_bad}"
    assert sum(err.count(n) for n in ErrMon.NAMES) == 0, err.pulses
    assert partner.dut_credit_violations == 0 and partner.parity_errors == 0 and partner.esc_errors == 0
    assert partner.link_resets == 0 and int(dut.running.value) == 1
    dut._log.info(f"random traffic OK: {len(exp_rx)} chars partner->DUT, {len(exp_tx)} chars DUT->partner, "
                  f"{partner.fcts} FCTs granted by DUT, {partner.fcts_sent} by partner")
    partner.stop()


@cocotb.test()
async def test_16_nchar_before_run_reports_errcred(dut):
    """ECSS 8.5.2.3-8.5.2.6: an N-char received in ErrorWait/Ready/Started/
    Connecting causes a silent transition to ErrorReset. Check which error
    output (if any) the core raises. Partner is a misbehaving node that sends
    NULL NULL DATA while the DUT is still in ErrorWait (never past Ready)."""
    partner, app = await setup(dut)
    err = ErrMon(dut)
    partner.enforce_credit = False
    partner.auto_fct = False
    # DUT: no linkstart/autostart -> ErrorReset (6.4us) -> ErrorWait (12.8us) -> Ready forever
    await Timer(8 * US, unit="ns")                   # DUT now in ErrorWait, receiver enabled
    # a character is only decoded once the following character's parity bit
    # arrived, so a NULL follows the data byte
    partner.send(("null",), ("null",), ("data", 0x42), ("null",))
    await partner.flush()
    await Timer(3 * US, unit="ns")
    ev = {n: err.events(n) for n in ErrMon.NAMES}
    dut._log.info(f"N-char in ErrorWait -> error pulses: { {k: len(v) for k, v in ev.items()} } "
                  f"started={int(dut.started.value)} running={int(dut.running.value)}")
    assert int(dut.rxvalid.value) == 0, "the character must not be delivered"
    assert len(ev["errdisc"]) == 0 and len(ev["errpar"]) == 0 and len(ev["erresc"]) == 0
    if ev["errcred"]:
        dut._log.warning("FINDING: errcred is asserted for an N-char received before the link is in "
                         "Run (here: ErrorWait). ECSS defines this as a silent reset, and the sibling "
                         "outputs errdisc/errpar/erresc are gated to Run. Application error counters "
                         "will attribute a link-restart glitch to a flow-control failure.")
    # same with the link established, then broken by the partner via disconnect, then a stale char
    partner.stop()
