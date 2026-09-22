# SpaceWire Light — independent verification (cocotb + GHDL + SymbiYosys)

**Türkçe özet.** Bu depo, açık kaynak SpaceWire Light çekirdeğinin (`spwstream`, ECSS-E-ST-50-12C)
bağımsız doğrulamasını içerir: Python tarafında bit seviyesinde bir SpaceWire link partneri ile
17 cocotb testi, `spwlink` durum makinesi için 42 PSL özellikli bir formal harness ve iki sayfalık
bir bulgu raporu. Bulgular: bir ECSS zamanlama uyumsuzluğu (20 MHz'de disconnect timeout 1093 ns,
sınır 1000 ns), bir protokol tehlikesi (link hatası + `linkdis` sonrası yarım paketin kuyruğunun
yeni paket gibi gönderilmesi), iki hata raporlama tutarsızlığı ve üç doküman eksiği.
Rapor: [`report/REPORT.md`](report/REPORT.md).

## What is here

| Path | Content |
|---|---|
| `report/REPORT.md` | Two-page verification report: findings with severity and evidence, ECSS timing measurements, method, reproduction |
| `tb/spw_bfm.py` | SpaceWire link partner: Data/Strobe encoder and decoder, parity and bit-period checking, credit accounting both ways, fault injection (bad parity, escape errors, credit violations, disconnect, phase shift) |
| `tb/test_spwstream.py` | 17 cocotb tests: link initialisation and timing, flow control, error injection and recovery, time-codes, discard semantics, FIFO limits, randomised soak |
| `tb/tb_top.vhd`, `tb/Makefile` | Integer-generic wrapper and GHDL flow (`SYSFREQ_KHZ`, `RXFIFO_BITS`, `TXFIFO_BITS` selectable) |
| `formal/spwlink_formal.vhd`, `formal/spwlink.sby` | Formal harness for the exchange-level controller: 12 assumptions from the `spwpkg` interface contracts, 42 assertions, 6 cover points; BMC, PDR and k-induction tasks |
| `setup.sh`, `env.sh`, `run_all.sh` | Root-free environment setup (OSS CAD Suite + cocotb venv) and one-shot regression |

The DUT itself is not vendored (GPL/LGPL); `setup.sh` clones the freecores mirror of
SpaceWire Light next to this repository. Everything here is MIT.

## Run

```
./setup.sh && source ./env.sh
make -C tb                                   # 17 tests, about 10 s on a laptop
make -C tb WAVES=1 COCOTB_TESTCASE=test_12_linkdis_cancels_discard   # with waveform (tb/dump.ghw)
cd formal && sby -f spwlink.sby pdr cover     # unbounded proof + reachability, seconds
```

## Results at a glance

* 16 of 17 tests pass; the one failing test is the ECSS disconnect-timeout check and is meant to fail on this version.
* Formal: PDR (IC3, ABC) proves all 42 assertions unboundedly in 3 s under the 12 interface assumptions; all 6 cover points reached (BMC depth 40); k-induction not needed
* One suspected flow-control bug from code review was disproved by a 16-phase directed sweep and by formal proof; the report documents both.

## Tooling

GHDL 7.0 (OSS CAD Suite 2026-09-22), cocotb 2.1.0, Yosys 0.69, SymbiYosys 0.69, Yices 2.7, ABC.
