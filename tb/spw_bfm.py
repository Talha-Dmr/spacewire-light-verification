"""
spw_bfm.py -- SpaceWire Data/Strobe bus-functional model for cocotb.

SpwLinkPartner behaves like the remote end of a SpaceWire link
(ECSS-E-ST-50-12C character/exchange level):

  * TX side (drives spw_di / spw_si into the DUT): sends NULLs when idle,
    honours the credit granted by the DUT, grants credit (FCT) to the DUT,
    and can inject faults (bad parity, illegal escape sequences, disconnect,
    credit violations).
  * RX side (decodes spw_do / spw_so from the DUT): character decoder with
    parity checking, bit-period checking, credit accounting for what the
    DUT is allowed to send, and link-reset detection.

Everything is observable from the outside of the DUT only (black-box).
"""
import cocotb
from cocotb.triggers import RisingEdge, Timer, Event
from cocotb.utils import get_sim_time
from cocotb.queue import Queue
from dataclasses import dataclass

NULL, FCT, EOP, EEP, DATA, TIMECODE, PARITY_ERR, ESC_ERR = (
    "NULL", "FCT", "EOP", "EEP", "DATA", "TC", "PERR", "EERR")


@dataclass
class Token:
    kind: str
    value: int = 0
    t_ns: float = 0.0

    def __repr__(self):
        if self.kind == DATA:
            return f"DATA(0x{self.value:02x})@{self.t_ns:.0f}"
        if self.kind == TIMECODE:
            return f"TC(ctrl={self.value >> 6},time={self.value & 0x3f})@{self.t_ns:.0f}"
        return f"{self.kind}@{self.t_ns:.0f}"


# ----------------------------------------------------------------------------
# Character level encoder / decoder
# ----------------------------------------------------------------------------
class SpwEncoder:
    """Produces bit lists for SpaceWire characters with correct odd parity.
    Parity bit covers the previous character's data bits plus the current
    parity and control bits (ECSS-E-ST-50-12C 7.4)."""

    def __init__(self):
        self.parity_acc = 0

    def reset(self):
        self.parity_acc = 0

    def char(self, control, data_bits, flip_parity=False):
        p = self.parity_acc ^ control ^ 1
        if flip_parity:
            p ^= 1
        acc = 0
        for b in data_bits:
            acc ^= b
        self.parity_acc = acc
        return [p, control] + list(data_bits)

    def fct(self, **kw):  return self.char(1, [0, 0], **kw)
    def eop(self, **kw):  return self.char(1, [0, 1], **kw)
    def eep(self, **kw):  return self.char(1, [1, 0], **kw)
    def esc(self, **kw):  return self.char(1, [1, 1], **kw)
    def data(self, byte, **kw):
        return self.char(0, [(byte >> i) & 1 for i in range(8)], **kw)
    def null(self):
        return self.esc() + self.fct()
    def timecode(self, ctrl, time):
        return self.esc() + self.data(((ctrl & 3) << 6) | (time & 0x3f))


class SpwDecoder:
    """Bit-serial SpaceWire character decoder. Synchronises on the first
    NULL (bit pattern 01110100), then checks parity on every character."""

    def __init__(self, on_token):
        self.on_token = on_token
        self.reset()

    def reset(self):
        self.synced = False
        self.window = []
        self.p = None
        self.c = None
        self.bits = []
        self.need = 0
        self.parity_acc = 0
        self.escaped = False

    def feed(self, bit, t_ns):
        if not self.synced:
            self.window = (self.window + [bit])[-8:]
            if self.window == [0, 1, 1, 1, 0, 1, 0, 0]:
                self.synced = True
                self.parity_acc = 0
                self.escaped = False
                self.on_token(Token(NULL, 0, t_ns))
            return
        if self.p is None:
            self.p = bit
            return
        if self.c is None:
            self.c = bit
            if (self.parity_acc ^ self.p ^ self.c) != 1:
                self.on_token(Token(PARITY_ERR, 0, t_ns))
            self.need = 2 if self.c == 1 else 8
            self.bits = []
            return
        self.bits.append(bit)
        if len(self.bits) < self.need:
            return
        acc = 0
        for b in self.bits:
            acc ^= b
        self.parity_acc = acc
        if self.c == 1:
            b0, b1 = self.bits
            code = (b1 << 1) | b0          # b0 is first on the wire
            if code == 0:                  # FCT
                if self.escaped:
                    self.escaped = False
                    self.on_token(Token(NULL, 0, t_ns))
                else:
                    self.on_token(Token(FCT, 0, t_ns))
            elif code == 3:                # ESC
                if self.escaped:
                    self.on_token(Token(ESC_ERR, 0, t_ns))
                self.escaped = True
            else:                          # EOP (code 2) / EEP (code 1)
                kind = EOP if code == 2 else EEP
                if self.escaped:
                    self.escaped = False
                    self.on_token(Token(ESC_ERR, 0, t_ns))
                else:
                    self.on_token(Token(kind, 0, t_ns))
        else:
            val = 0
            for i, b in enumerate(self.bits):
                val |= b << i
            if self.escaped:
                self.escaped = False
                self.on_token(Token(TIMECODE, val, t_ns))
            else:
                self.on_token(Token(DATA, val, t_ns))
        self.p = None
        self.c = None
        self.bits = []


# ----------------------------------------------------------------------------
# Link partner
# ----------------------------------------------------------------------------
def _bit(sig):
    try:
        return int(sig.value)
    except (ValueError, TypeError):
        return 0


class SpwLinkPartner:
    DISCONNECT_NS = 850.0

    def __init__(self, dut, bit_period_ns=100.0, log=None):
        self.dut = dut
        self.clk = dut.clk
        self.di, self.si = dut.spw_di, dut.spw_si
        self.do, self.so = dut.spw_do, dut.spw_so
        self.bit_ns = bit_period_ns
        self.log = log or dut._log
        # --- TX side
        self.enc = SpwEncoder()
        self.d = 0
        self.s = 0
        self.queue = Queue()
        self.busy = False
        self.idle_nulls = False
        self.enforce_credit = True
        self.auto_fct = True
        self.max_outstanding = 56
        self.tx_credit = 0
        self.credit_event = Event()
        self.sent_null = False
        self.chars_sent = 0
        self.fcts_sent = 0
        # --- RX side
        self.dec = SpwDecoder(self._on_token)
        self.tokens = []
        self.rx_chars = []          # ('D', byte) | ('EOP',) | ('EEP',)
        self.rx_timecodes = []
        self.nulls = 0
        self.fcts = 0
        self.outstanding = 0        # credit granted to DUT, not yet used
        self.granted_total = 0
        self.dut_nchars = 0
        self.saw_dut_null = False
        self.dut_credit_violations = 0
        self.parity_errors = 0
        self.esc_errors = 0
        self.expected_bit_ns = None
        self.bit_period_errors = 0
        self.last_transition_ns = None
        self.link_resets = 0
        self.token_event = Event()
        self.reset_event = Event()
        self._tasks = []
        self.di.value = 0
        self.si.value = 0

    # ---------------- lifecycle
    def start(self):
        self._tasks.append(cocotb.start_soon(self._tx_loop()))
        self._tasks.append(cocotb.start_soon(self._rx_loop()))

    def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    def clear_stats(self):
        """Forget everything observed so far (call after a DUT reset)."""
        self.tokens = []
        self.rx_chars = []
        self.rx_timecodes = []
        self.nulls = self.fcts = 0
        self.granted_total = self.fcts_sent = self.chars_sent = 0
        self.dut_nchars = 0
        self.dut_credit_violations = self.parity_errors = self.esc_errors = 0
        self.bit_period_errors = 0
        self.link_resets = 0

    def reset_link_state(self, why=""):
        """Called when the DUT resets its link (transmitter disabled)."""
        self.link_resets += 1
        self.tx_credit = 0
        self.outstanding = 0
        self.saw_dut_null = False
        self.dec.reset()
        self.enc.reset()
        self.sent_null = False
        self.reset_event.set()
        self.log.info(f"[partner] DUT link reset detected ({why}) at {get_sim_time(unit='ns'):.0f} ns")

    # ---------------- TX helpers (enqueue)
    def send(self, *items):
        for it in items:
            self.queue.put_nowait(it)

    def send_packet(self, payload, end="eop"):
        for b in payload:
            self.queue.put_nowait(("data", b))
        if end:
            self.queue.put_nowait((end,))

    async def flush(self):
        while not (self.queue.empty() and not self.busy):
            await Timer(self.bit_ns, unit="ns")

    # ---------------- TX implementation
    async def _send_bits(self, bits):
        for b in bits:
            if b == self.d:
                self.s ^= 1
            else:
                self.d = b
            self.di.value = self.d
            self.si.value = self.s
            self.last_tx_transition_ns = get_sim_time(unit="ns")
            await Timer(self.bit_ns, unit="ns")

    def _fct_due(self):
        return (self.auto_fct and self.saw_dut_null and self.sent_null
                and self.outstanding + 8 <= self.max_outstanding)

    async def _send_fct(self):
        self.outstanding += 8
        self.granted_total += 8
        self.fcts_sent += 1
        await self._send_bits(self.enc.fct())

    async def _wait_credit(self):
        """Wait for credit from the DUT while keeping the link alive
        (NULLs and due FCTs continue to flow, as on a real link)."""
        while self.enforce_credit and self.tx_credit <= 0:
            if self._fct_due():
                await self._send_fct()
            elif self.idle_nulls:
                self.sent_null = True
                await self._send_bits(self.enc.null())
            else:
                await Timer(self.bit_ns, unit="ns")

    async def _do_item(self, item):
        kind = item[0]
        if kind == "data":
            await self._wait_credit()
            self.tx_credit -= 1
            self.chars_sent += 1
            await self._send_bits(self.enc.data(item[1], flip_parity=(len(item) > 2 and item[2])))
        elif kind in ("eop", "eep"):
            await self._wait_credit()
            self.tx_credit -= 1
            self.chars_sent += 1
            flip = len(item) > 1 and item[1]
            bits = self.enc.eop(flip_parity=flip) if kind == "eop" else self.enc.eep(flip_parity=flip)
            await self._send_bits(bits)
        elif kind == "null":
            self.sent_null = True
            await self._send_bits(self.enc.null())
        elif kind == "fct":
            await self._send_fct()
        elif kind == "esc":
            await self._send_bits(self.enc.esc())
        elif kind == "tc":
            await self._send_bits(self.enc.timecode(item[1], item[2]))
        elif kind == "raw":
            await self._send_bits(list(item[1]))
        elif kind == "pause":            # no transitions for item[1] ns
            if item[1] > 0:
                await Timer(item[1], unit="ns")
        elif kind == "glitch":           # simultaneous D and S transition
            self.d ^= 1
            self.s ^= 1
            self.di.value = self.d
            self.si.value = self.s
            await Timer(self.bit_ns, unit="ns")
        elif kind == "event":
            item[1].set()
        else:
            raise ValueError(f"unknown item {item}")

    async def _tx_loop(self):
        await Timer(7, unit="ns")          # keep D/S transitions off the clock edges
        while True:
            if self._fct_due():
                await self._send_fct()
                continue
            if self.queue.empty():
                if self.idle_nulls:
                    self.sent_null = True
                    await self._send_bits(self.enc.null())
                else:
                    await Timer(self.bit_ns, unit="ns")
                continue
            item = self.queue.get_nowait()
            self.busy = True
            try:
                await self._do_item(item)
            finally:
                self.busy = False

    # ---------------- RX implementation
    def _on_token(self, tok):
        self.tokens.append(tok)
        if tok.kind == NULL:
            self.nulls += 1
            self.saw_dut_null = True
        elif tok.kind == FCT:
            self.fcts += 1
            self.tx_credit += 8
            self.credit_event.set()
        elif tok.kind in (DATA, EOP, EEP):
            self.dut_nchars += 1
            self.outstanding -= 1
            if self.outstanding < 0:
                self.dut_credit_violations += 1
            self.rx_chars.append(("D", tok.value) if tok.kind == DATA else (tok.kind,))
        elif tok.kind == TIMECODE:
            self.rx_timecodes.append((tok.value >> 6, tok.value & 0x3f, tok.t_ns))
        elif tok.kind == PARITY_ERR:
            self.parity_errors += 1
        elif tok.kind == ESC_ERR:
            self.esc_errors += 1
        self.token_event.set()

    def _dut_tx_enabled(self):
        return _bit(self.dut.started) or _bit(self.dut.connecting) or _bit(self.dut.running)

    async def _rx_loop(self):
        prev_d, prev_s = _bit(self.do), _bit(self.so)
        prev_en = self._dut_tx_enabled()
        while True:
            await RisingEdge(self.clk)
            t = get_sim_time(unit="ns")
            en = self._dut_tx_enabled()
            if prev_en and not en:
                self.reset_link_state("transmitter disabled")
            prev_en = en
            d, s = _bit(self.do), _bit(self.so)
            if (d ^ s) != (prev_d ^ prev_s):
                if self.last_transition_ns is not None and self.expected_bit_ns is not None:
                    dt = t - self.last_transition_ns
                    if abs(dt - self.expected_bit_ns) > 1.0:
                        self.bit_period_errors += 1
                self.last_transition_ns = t
                if en:
                    self.dec.feed(d, t)
            elif (self.dec.synced and self.last_transition_ns is not None
                  and t - self.last_transition_ns > self.DISCONNECT_NS):
                self.reset_link_state("DUT silent")
            prev_d, prev_s = d, s

    # ---------------- waiting helpers
    async def wait_token(self, kind, timeout_ns=100_000.0, since=None):
        """Wait until a token of `kind` appears after index `since`."""
        start = len(self.tokens) if since is None else since
        t0 = get_sim_time(unit="ns")
        while True:
            for tok in self.tokens[start:]:
                if tok.kind == kind:
                    return tok
            start = len(self.tokens)
            if get_sim_time(unit="ns") - t0 > timeout_ns:
                raise TimeoutError(f"no {kind} token from DUT within {timeout_ns} ns")
            self.token_event.clear()
            await self.token_event.wait()

    async def wait_rx_chars(self, n, timeout_ns=1_000_000.0):
        t0 = get_sim_time(unit="ns")
        while len(self.rx_chars) < n:
            if get_sim_time(unit="ns") - t0 > timeout_ns:
                raise TimeoutError(f"DUT sent {len(self.rx_chars)} of {n} expected chars")
            self.token_event.clear()
            await self.token_event.wait()
        return self.rx_chars[:n]
