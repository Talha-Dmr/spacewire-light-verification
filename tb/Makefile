# cocotb testbench for SpaceWire Light "spwstream" (GHDL)
SIM ?= ghdl
TOPLEVEL_LANG ?= vhdl

RTL := $(abspath ../spacewire_light/rtl/vhdl)
VHDL_SOURCES := $(RTL)/spwpkg.vhd $(RTL)/spwlink.vhd $(RTL)/spwrecv.vhd \
                $(RTL)/spwxmit.vhd $(RTL)/spwxmit_fast.vhd \
                $(RTL)/spwrecvfront_generic.vhd $(RTL)/spwrecvfront_fast.vhd \
                $(RTL)/syncdff.vhd $(RTL)/spwram.vhd $(RTL)/spwstream.vhd $(abspath tb_top.vhd)

COCOTB_TOPLEVEL := tb_top
COCOTB_TEST_MODULES ?= test_spwstream

# Design generics (override on the command line, e.g. make RXFIFO_BITS=6)
SYSFREQ_KHZ ?= 20000
RXFIFO_BITS ?= 6
TXFIFO_BITS ?= 6

GHDL_ARGS += --std=08 -frelaxed
SIM_ARGS  += -gsysfreq_khz=$(SYSFREQ_KHZ) -grxfifosize_bits=$(RXFIFO_BITS) -gtxfifosize_bits=$(TXFIFO_BITS)
ifeq ($(WAVES),1)
SIM_ARGS  += --wave=dump.ghw
endif

export SYSFREQ_KHZ RXFIFO_BITS TXFIFO_BITS
include $(shell cocotb-config --makefiles)/Makefile.sim
