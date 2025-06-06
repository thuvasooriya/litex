#
# This file is part of LiteX.
#
# Copyright (c) 2014-2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2014-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2016-2018 Robert Jordens <jordens@gmail.com>
# Copyright (c) 2015 William D. Jones <thor0505@comcast.net>
# SPDX-License-Identifier: BSD-2-Clause

import os
import sys
import subprocess

from migen.fhdl.structure import *
from migen.fhdl.specials import Instance, Tristate
from migen.fhdl.module import Module
from migen.genlib.cdc import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.build.io import *
from litex.build import tools
from litex.build.yosys_wrapper import YosysWrapper

# Colorama -----------------------------------------------------------------------------------------

try:
    import colorama
    colorama.init()  # install escape sequence translation on Windows
    _have_colorama = True
except ImportError:
    _have_colorama = False

colors = []
if _have_colorama:
    colors += [
        ("^ERROR:.*$", colorama.Fore.RED + colorama.Style.BRIGHT +
         r"\g<0>" + colorama.Style.RESET_ALL),
        ("^CRITICAL WARNING:.*$", colorama.Fore.RED +
         r"\g<0>" + colorama.Style.RESET_ALL),
        ("^WARNING:.*$", colorama.Fore.YELLOW +
         r"\g<0>" + colorama.Style.RESET_ALL),
        ("^INFO:.*$", colorama.Fore.GREEN +
         r"\g<0>" + colorama.Style.RESET_ALL),
    ]

# Common MultiReg ----------------------------------------------------------------------------------

class XilinxMultiRegImpl(MultiRegImpl):
    def __init__(self, *args, **kwargs):
        MultiRegImpl.__init__(self, *args, **kwargs)
        i = self.i
        if not hasattr(i, "attr"):
            i0, i = i, Signal()
            self.comb += i.eq(i0)
        if len(self.regs):
            self.regs[0].attr.add("mr_ff")
        for r in self.regs:
            r.attr.add("async_reg")
            r.attr.add("no_shreg_extract")


class XilinxMultiReg:
    @staticmethod
    def lower(dr):
        return XilinxMultiRegImpl(dr.i, dr.o, dr.odomain, dr.n)

# Common AsyncResetSynchronizer --------------------------------------------------------------------

class XilinxAsyncResetSynchronizerImpl(Module):
    def __init__(self, cd, async_reset):
        if not hasattr(async_reset, "attr"):
            i, async_reset = async_reset, Signal()
            self.comb += async_reset.eq(i)
        rst_buf  = Signal()
        rst_meta = Signal()
        self.specials += [
            Instance("FDPE",
                attr   = {"async_reg", "ars_ff1"},
                p_INIT = 1,
                i_PRE  = async_reset,
                i_CE   = 1,
                i_C    = cd.clk,
                i_D    = 0,
                o_Q    = rst_meta,
            ),
            Instance("FDPE",
                attr   = {"async_reg", "ars_ff2"},
                p_INIT = 1,
                i_PRE  = async_reset,
                i_CE   = 1,
                i_C    = cd.clk,
                i_D    = rst_meta,
                o_Q    = cd.rst if getattr(cd, "rst_buf", None) is None else rst_buf
            )
        ]
        # Add optional BUFG.
        if getattr(cd, "rst_buf", None) is not None:
            self.specials += Instance("BUFG", i_I=rst_buf,o_O= cd.rst)

class XilinxAsyncResetSynchronizer:
    @staticmethod
    def lower(dr):
        return XilinxAsyncResetSynchronizerImpl(dr.cd, dr.async_reset)

# Common DifferentialInput -------------------------------------------------------------------------

class XilinxDifferentialInputImpl(Module):
    def __init__(self, i_p, i_n, o):
        self.specials += Instance("IBUFDS",
            i_I  = i_p,
            i_IB = i_n,
            o_O  = o
        )


class XilinxDifferentialInput:
    @staticmethod
    def lower(dr):
        return XilinxDifferentialInputImpl(dr.i_p, dr.i_n, dr.o)

# Common DifferentialOutput ------------------------------------------------------------------------

class XilinxDifferentialOutputImpl(Module):
    def __init__(self, i, o_p, o_n):
        self.specials += Instance("OBUFDS",
            i_I  = i,
            o_O  = o_p,
            o_OB = o_n
        )


class XilinxDifferentialOutput:
    @staticmethod
    def lower(dr):
        return XilinxDifferentialOutputImpl(dr.i, dr.o_p, dr.o_n)

# Common SDRTristate -------------------------------------------------------------------------------

class XilinxSDRTristateImpl(Module):
    def __init__(self, io, o, oe, i, clk):
        _o    = Signal().like(o)
        _oe_n = Signal().like(oe)
        _i    = Signal().like(i)
        self.specials += SDROutput(o, _o, clk)
        self.specials += SDROutput(~oe, _oe_n, clk)
        self.specials += SDRInput(_i, i, clk)
        for j in range(len(io)):
            self.specials += Instance("IOBUF",
                io_IO = io[j],
                o_O   = _i[j],
                i_I   = _o[j],
                i_T   = _oe_n[j],
            )

class XilinxSDRTristate:
    @staticmethod
    def lower(dr):
        return XilinxSDRTristateImpl(dr.io, dr.o, dr.oe, dr.i, dr.clk)

# Common DDRTristate -------------------------------------------------------------------------------

class XilinxDDRTristateImpl(Module):
    def __init__(self, io, o1, o2, oe1, oe2, i1, i2, clk, i_async):
        _o    = Signal().like(o1)
        _oe_n = Signal().like(oe1)
        _i    = Signal().like(i1)
        self.specials += DDROutput(o1, o2, _o, clk)
        self.specials += DDROutput(~oe1, ~oe2, _oe_n, clk) if oe2 is not None else SDROutput(~oe1, _oe_n, clk)
        self.specials += DDRInput(_i, i1, i2, clk)
        for j in range(len(io)):
            self.specials += Instance("IOBUF",
                io_IO = io[j],
                o_O   = _i[j],
                i_I   = _o[j],
                i_T   = _oe_n[j],
            )
        self.comb += i_async.eq(_i)

class XilinxDDRTristate:
    @staticmethod
    def lower(dr):
        return XilinxDDRTristateImpl(dr.io, dr.o1, dr.o2, dr.oe1, dr.oe2, dr.i1, dr.i2, dr.clk, dr.i_async)

# Common Special Overrides -------------------------------------------------------------------------

xilinx_special_overrides = {
    MultiReg:               XilinxMultiReg,
    AsyncResetSynchronizer: XilinxAsyncResetSynchronizer,
    DifferentialInput:      XilinxDifferentialInput,
    DifferentialOutput:     XilinxDifferentialOutput,
    SDRTristate:            XilinxSDRTristate,
    DDRTristate:            XilinxDDRTristate,
}

# Spartan6 DDROutput -------------------------------------------------------------------------------

class XilinxDDROutputImplS6(Module):
    def __init__(self, i1, i2, o, clk):
        for j in range(len(o)):
            self.specials += Instance("ODDR2",
                p_DDR_ALIGNMENT = "C0",
                p_INIT          = 0,
                p_SRTYPE        = "ASYNC",
                i_C0 = clk,
                i_C1 = ~clk,
                i_CE = 1,
                i_S  = 0,
                i_R  = 0,
                i_D0 = i1[j],
                i_D1 = i2[j],
                o_Q  = o[j]
            )


class XilinxDDROutputS6:
    @staticmethod
    def lower(dr):
        return XilinxDDROutputImplS6(dr.i1, dr.i2, dr.o, dr.clk)

# Spartan6 DDRInput --------------------------------------------------------------------------------

class XilinxDDRInputImplS6(Module):
    def __init__(self, i, o1, o2, clk):
        for j in range(len(i)):
            self.specials += Instance("IDDR2",
                p_DDR_ALIGNMENT = "C0",
                p_INIT_Q0       = 0,
                p_INIT_Q1       = 0,
                p_SRTYPE        = "ASYNC",
                i_C0 = clk,
                i_C1 = ~clk,
                i_CE = 1,
                i_S  = 0,
                i_R  = 0,
                i_D  = i[j],
                o_Q0 = o1[j],
                o_Q1 = o2[j]
            )


class XilinxDDRInputS6:
    @staticmethod
    def lower(dr):
        return XilinxDDRInputImplS6(dr.i, dr.o1, dr.o2, dr.clk)

# Spartan6 SDROutput -------------------------------------------------------------------------------

class XilinxSDROutputS6:
    @staticmethod
    def lower(dr):
        return XilinxDDROutputImplS6(dr.i, dr.i, dr.o, dr.clk)


# Spartan6 SDRInput --------------------------------------------------------------------------------

class XilinxSDRInputS6:
    @staticmethod
    def lower(dr):
        return XilinxDDRInputImplS6(dr.i, dr.o, Signal(len(dr.o)), dr.clk)

# Spartan6 Special Overrides -----------------------------------------------------------------------

xilinx_s6_special_overrides = {
    DDROutput:   XilinxDDROutputS6,
    DDRInput:    XilinxDDRInputS6,
    SDROutput:   XilinxSDROutputS6,
    SDRInput:    XilinxSDRInputS6,
}

# 7-Series DDROutput -------------------------------------------------------------------------------

class XilinxDDROutputImplS7(Module):
    def __init__(self, i1, i2, o, clk):
        for j in range(len(o)):
            self.specials += Instance("ODDR",
                p_DDR_CLK_EDGE="SAME_EDGE",
                i_C  = clk,
                i_CE = 1,
                i_S  = 0,
                i_R  = 0,
                i_D1 = i1[j],
                i_D2 = i2[j],
                o_Q  = o[j]
            )


class XilinxDDROutputS7:
    @staticmethod
    def lower(dr):
        return XilinxDDROutputImplS7(dr.i1, dr.i2, dr.o, dr.clk)

# 7-Series DDRInput --------------------------------------------------------------------------------

class XilinxDDRInputImplS7(Module):
    def __init__(self, i, o1, o2, clk):
        for j in range(len(i)):
            self.specials += Instance("IDDR",
                p_DDR_CLK_EDGE="SAME_EDGE",
                i_C  = clk,
                i_CE = 1,
                i_S  = 0,
                i_R  = 0,
                i_D  = i[j],
                o_Q1 = o1[j],
                o_Q2 = o2[j]
            )


class XilinxDDRInputS7:
    @staticmethod
    def lower(dr):
        return XilinxDDRInputImplS7(dr.i, dr.o1, dr.o2, dr.clk)

# 7-Series SDROutput -------------------------------------------------------------------------------

class XilinxSDROutputS7:
    @staticmethod
    def lower(dr):
        return XilinxDDROutputImplS7(dr.i, dr.i, dr.o, dr.clk)


# 7-Series SDRInput --------------------------------------------------------------------------------

class XilinxSDRInputS7:
    @staticmethod
    def lower(dr):
        return XilinxDDRInputImplS7(dr.i, dr.o, Signal(len(dr.o)), dr.clk)

# 7-Series Special Overrides -----------------------------------------------------------------------

xilinx_s7_special_overrides = {
    DDROutput: XilinxDDROutputS7,
    DDRInput:  XilinxDDRInputS7,
    SDROutput: XilinxSDROutputS7,
    SDRInput:  XilinxSDRInputS7,
}

# Ultrascale DDROutput -----------------------------------------------------------------------------

class XilinxDDROutputImplUS(Module):
    def __init__(self, i1, i2, o, clk):
        for j in range(len(o)):
            self.specials += Instance("ODDRE1",
                i_C  = clk,
                i_SR = 0,
                i_D1 = i1[j],
                i_D2 = i2[j],
                o_Q  = o[j]
            )


class XilinxDDROutputUS:
    @staticmethod
    def lower(dr):
        return XilinxDDROutputImplUS(dr.i1, dr.i2, dr.o, dr.clk)

# Ultrascale DDRInput ------------------------------------------------------------------------------

class XilinxDDRInputImplUS(Module):
    def __init__(self, i, o1, o2, clk):
        for j in range(len(i)):
            self.specials += Instance("IDDRE1",
                p_DDR_CLK_EDGE="SAME_EDGE_PIPELINED",
                p_IS_C_INVERTED  = 0,
                p_IS_CB_INVERTED = 1,
                i_C  = clk,
                i_CB = clk,
                i_R  = 0,
                i_D  = i[j],
                o_Q1 = o1[j],
                o_Q2 = o2[j]
            )


class XilinxDDRInputUS:
    @staticmethod
    def lower(dr):
        return XilinxDDRInputImplUS(dr.i, dr.o1, dr.o2, dr.clk)

# Ultrascale SDROutput -----------------------------------------------------------------------------

class XilinxSDROutputImplUS(Module):
    def __init__(self, i, o, clk):
        for j in range(len(o)):
            self.specials += Instance("FDCE",
                i_C   = clk,
                i_CE  = 1,
                i_CLR = 0,
                i_D   = i[j],
                o_Q   = o[j]
            )

class XilinxSDROutputUS:
    @staticmethod
    def lower(dr):
        return XilinxSDROutputImplUS(dr.i, dr.o, dr.clk)
        
# Ultrascale SDRInput ------------------------------------------------------------------------------
class XilinxSDRInputImplUS(Module):
    def __init__(self, i, o, clk): 
        for j in range(len(i)):
            self.specials += Instance("FDCE",
                i_C   = clk,
                i_CE  = 1,
                i_CLR = 0,
                i_D   = i[j],
                o_Q   = o[j]
            )

class XilinxSDRInputUS:
    @staticmethod
    def lower(dr):
        return XilinxSDRInputImplUS(dr.i, dr.o, dr.clk)

# Ultrascale Specials Overrides --------------------------------------------------------------------

xilinx_us_special_overrides = {
    DDROutput: XilinxDDROutputUS,
    DDRInput:  XilinxDDRInputUS,
    SDROutput: XilinxSDROutputUS,
    SDRInput:  XilinxSDRInputUS,
}

# Yosys Run ----------------------------------------------------------------------------------------

def _build_yosys_project(platform, synth_opts="", build_name=""):
    family = ""
    device = platform.device
    if (device.startswith("xc7") or device.startswith("xa7") or device.startswith("xq7")):
        family = "xc7"
    elif (device.startswith("xc6s") or device.startswith("xa6s") or device.startswith("xq6s")):
        family = "xc6s"
    else:
        raise OSError("Unsupported device")

    yosys_cmd = [
        "hierarchy -top {build_name}",
        "# FIXME: Are these needed?",
        "# proc; memory; opt; fsm; opt",
        "# Map keep to keep=1 for yosys",
        "log",
        "log XX. Converting (* keep = \"xxxx\" *) attribute for Yosys",
        "log",
        "attrmap -tocase keep -imap keep=\"true\" keep=1 -imap keep=\"false\" keep=0 -remove keep=0",
        "select -list a:keep=1",
        "# Add keep=1 for yosys to objects which have dont_touch=\"true\" attribute.",
        "log",
        "log XX. Converting (* dont_touch = \"true\" *) attribute for Yosys",
        "log",
        "select -list a:dont_touch=true",
        "setattr -set keep 1 a:dont_touch=true",
        "# Convert (* async_reg = \"true\" *) to async registers for Yosys.",
        "# (* async_reg = \"true\", dont_touch = \"true\" *) reg xilinxmultiregimpl0_regs1 = 1'd0;",
        "log",
        "log XX. Converting (* async_reg = \"true\" *) attribute to async registers for Yosys",
        "log",
        "select -list a:async_reg=true",
        "setattr -set keep 1 a:async_reg=true",
    ]

    yosys = YosysWrapper(platform, build_name,
        target       = "xilinx",
        template     = [],
        yosys_cmds   = yosys_cmd,
        yosys_opts   = f"-family {family}",
        synth_format = "edif"
    )
    yosys.build_script()
    return yosys.get_yosys_call("script") + "\n"
