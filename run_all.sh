#!/usr/bin/env bash
# Full regression: cocotb tests (20 MHz + timing at 40/100 MHz) and formal.
set -uo pipefail
cd "$(dirname "$0")"
source ./env.sh
echo "== cocotb regression (20 MHz, 64-byte FIFOs) =="
make -C tb 2>&1 | grep -E "\*\* |FINDING"
for f in 40000 100000; do
  echo "== ECSS timing at ${f} kHz =="
  make -C tb SYSFREQ_KHZ=$f SIM_BUILD=sim_build_$f COCOTB_RESULTS_FILE=results_$f.xml \
       COCOTB_TESTCASE=test_02_link_init_timing,test_06b_disconnect_timeout_ecss_window 2>&1 \
       | grep -E "errdisc .* after|reset -> Started|Started timeout|\*\* test"
done
echo "== formal (spwlink) =="
(cd formal && sby -f spwlink.sby bmc3 cover pdr 2>&1 | grep -E "summary: (engine|failed|counter)|DONE")
