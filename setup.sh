#!/usr/bin/env bash
# One-shot environment setup (no root needed).
#   - OSS CAD Suite (GHDL, Yosys, SymbiYosys, solvers)  -> ./tools/oss-cad-suite
#   - Python venv with cocotb                            -> ./.venv
#   - DUT: SpaceWire Light (freecores mirror)            -> ./spacewire_light
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d spacewire_light ]; then
  git clone --depth 1 https://github.com/freecores/spacewire_light.git
fi
if [ ! -d tools/oss-cad-suite ]; then
  mkdir -p tools
  TAG=$(curl -s https://api.github.com/repos/YosysHQ/oss-cad-suite-build/releases/latest | python3 -c 'import sys,json;print(json.load(sys.stdin)["tag_name"])')
  wget -q --show-progress -O tools/oss-cad-suite.tgz \
    "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${TAG}/oss-cad-suite-linux-x64-${TAG//-/}.tgz"
  tar xzf tools/oss-cad-suite.tgz -C tools && rm tools/oss-cad-suite.tgz
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q cocotb==2.1.0 pytest cocotb-coverage
fi
echo "OK. Activate with:  source ./env.sh"
