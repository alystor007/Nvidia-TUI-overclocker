#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 alystor007
"""fan_control.py — read/set the GPU fan speed (NVML).

The TUI shells out to this the same way it runs apply_overclock.py, so all
pynvml calls stay in this file (the TUI itself has no pynvml dependency).

Usage:
  fan_control.py            status (default) — print JSON and exit 0
  fan_control.py status     same as above
  fan_control.py set <PCT>  set a manual fan speed on ALL fans (needs root)
  fan_control.py auto       return all fans to the driver's auto curve (root)

Notes:
  * "All fans" = every index nvmlDeviceGetNumFans reports (on a 3-fan card
    NVML typically exposes 2 PWM channels; both are set, which covers every
    physical fan the driver reports).
  * set/auto need root (sudo) — the TUI runs them under sudo, so the
    subprocess inherits it. Non-root runs fail with a permission error.
  * status is read-only and works without root.
"""
import json
import sys
from pynvml import (
    NVMLError,
    NVML_FAN_POLICY_MANUAL,
    NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW,
    nvmlDeviceGetFanControlPolicy_v2,
    nvmlDeviceGetFanSpeed,
    nvmlDeviceGetFanSpeedRPM,
    nvmlDeviceGetMinMaxFanSpeed,
    nvmlDeviceGetNumFans,
    nvmlDeviceSetFanControlPolicy,
    nvmlDeviceSetFanSpeed_v2,
    nvmlDeviceGetHandleByIndex,
    nvmlInit,
    nvmlShutdown,
)

GPU_INDEX = 0   # primary GPU (same as apply_overclock.py)

POLICY_NAMES = {
    NVML_FAN_POLICY_MANUAL: "manual",
    NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW: "auto",
}


def _policy_name(p):
    return POLICY_NAMES.get(p, f"unknown({p})")


def status():
    """Read fan state (read-only, no root needed). Prints JSON, exits 0."""
    nvmlInit()
    try:
        gpu = nvmlDeviceGetHandleByIndex(GPU_INDEX)
        fans = nvmlDeviceGetNumFans(gpu)
        # Index 0 is representative: every PWM channel shares the auto target,
        # so a single read stands in for the card as a whole.
        speed = nvmlDeviceGetFanSpeed(gpu)
        policy = _policy_name(nvmlDeviceGetFanControlPolicy_v2(gpu, 0))
        try:
            rpm = nvmlDeviceGetFanSpeedRPM(gpu)
        except NVMLError:
            rpm = None
        try:
            mn, mx = nvmlDeviceGetMinMaxFanSpeed(gpu)
        except NVMLError:
            mn = mx = None
    finally:
        nvmlShutdown()
    out = {
        "speed": speed,
        "rpm": rpm,
        "policy": policy,
        "fans": fans,
        "min": mn,
        "max": mx,
    }
    print(json.dumps(out))


def set_speed(pct):
    """Set a manual fan speed on every fan. Best-effort per fan."""
    # validate against the allowed range, then apply to each reported fan
    nvmlInit()
    try:
        gpu = nvmlDeviceGetHandleByIndex(GPU_INDEX)
        try:
            mn, mx = nvmlDeviceGetMinMaxFanSpeed(gpu)
            if mn is not None and mx is not None and not (mn <= pct <= mx):
                sys.exit(f"[fan] {pct}% is out of range ({mn}-{mx}%)")
        except NVMLError:
            pass   # can't read range; let the set call report the real error
        indices = list(range(nvmlDeviceGetNumFans(gpu)))
        failures = []
        for i in indices:
            try:
                nvmlDeviceSetFanSpeed_v2(gpu, i, pct)
            except NVMLError as e:
                failures.append((i, str(e)))
        if failures:
            detail = "; ".join(f"fan{i}: {msg}" for i, msg in failures)
            sys.exit(f"[fan] set {pct}% failed ({detail})")
        print(f"[fan] manual {pct}% ({len(indices)} fan(s))")
    finally:
        nvmlShutdown()


def to_auto():
    """Return every fan to the driver's auto curve. Best-effort per fan."""
    nvmlInit()
    try:
        gpu = nvmlDeviceGetHandleByIndex(GPU_INDEX)
        indices = list(range(nvmlDeviceGetNumFans(gpu)))
        failures = []
        for i in indices:
            try:
                nvmlDeviceSetFanControlPolicy(gpu, i,
                                              NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW)
            except NVMLError as e:
                failures.append((i, str(e)))
        if failures:
            detail = "; ".join(f"fan{i}: {msg}" for i, msg in failures)
            sys.exit(f"[fan] auto failed ({detail})")
        print(f"[fan] auto ({len(indices)} fan(s))")
    finally:
        nvmlShutdown()


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd in ("status",):
        status()
    elif cmd == "set":
        if len(args) != 2:
            sys.exit("[fan] usage: fan_control.py set <PCT>")
        try:
            pct = int(args[1])
        except ValueError:
            sys.exit(f"[fan] not a number: {args[1]}")
        set_speed(pct)
    elif cmd == "auto":
        to_auto()
    else:
        sys.exit(f"[fan] unknown command: {cmd}")


if __name__ == "__main__":
    main()
