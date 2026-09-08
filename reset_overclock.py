#!/bin/env python
"""reset_overclock.py — remove overclocking: power cap, locked clocks, clock offsets.

NVML has no "0 / reset" value for these controls, so each one is restored
to its factory-default equivalent:
  * power limit -> 285 W (factory default)
  * locked clocks -> unlocked
  * mem / gfx clock offsets -> 0

Each step is best-effort: a step the driver rejects is reported, not fatal.
"""
import os
from ctypes import byref
from pynvml import *


def _step(desc, fn):
    """Run one reset step; report success/failure without aborting the rest."""
    try:
        fn()
        print(f"[resetoc] {desc}")
    except NVMLError as e:
        print(f"[resetoc][warn] {desc} failed: {e}")


nvmlInit()

# This sets the GPU to adjust - if this gives you errors or you have
# multiple GPUs, set to 1 or try other values.
myGPU = nvmlDeviceGetHandleByIndex(0)

# ---- power limit: 0 is invalid; restore the factory default (285 W) ----
FACTORY_POWER_W = 285

def _reset_power():
    mw = FACTORY_POWER_W * 1000
    nvmlDeviceSetPowerManagementLimit(myGPU, mw)
    print(f"  power limit -> {mw} mW (factory default)")


_step("power limit -> default", _reset_power)

# ---- locked clocks: unlock (0,0); some drivers reject this ----
_step("remove locked clocks", lambda: nvmlDeviceSetGpuLockedClocks(myGPU, 0, 0))

# ---- P0 state: clear mem + gfx clock offsets ----
def _clear_offset(ctype):
    def _do():
        info = c_nvmlClockOffset_t()
        info.version = nvmlClockOffset_v1
        info.type = ctype
        info.pstate = NVML_PSTATE_0
        info.clockOffsetMHz = 0
        nvmlDeviceSetClockOffsets(myGPU, byref(info))
    return _do


_step("clear mem clock offset", _clear_offset(NVML_CLOCK_MEM))
_step("clear gfx clock offset", _clear_offset(NVML_CLOCK_GRAPHICS))

nvmlShutdown()

# Remove the OC-active flag (created by apply_overclock.py).
try:
    os.remove("/tmp/gpu_oc_active")
except OSError:
    pass
