#!/usr/bin/env python3
"""
apply_overclock.py — applies an OC profile from profiles.json.

Single source of truth: ~/.config/gpu-tui/profiles.json
  {
    "selected": "default",
    "default": {
      "power_w":   260,   # power limit (W)
      "clock_min": 210,   # locked min clock (MHz)
      "clock_max": 2730,  # locked max clock (MHz)
      "mem_off":   400,   # memory clock offset (MHz)
      "gfx_off":   200    # graphics clock offset (MHz)
    }
  }

The .json is read-only input (only rewritten when a different profile name
is passed on the command line). After a successful apply, the active profile
name is written to /tmp/gpu_oc_active (removed by reset_overclock.py).

Usage:
  apply_overclock.py             apply the selected profile
  apply_overclock.py <profile>   apply <profile> and make it selected
  apply_overclock.py --list      list profiles ('*' = selected)

Falls back to DEFAULTS below when no config exists (standalone first run).
"""
import json
import os
import sys
from ctypes import byref
from pynvml import *

# Under sudo, $HOME is /root — use the invoking user's home (SUDO_HOME)
# so the config path is the same with and without sudo (see gpu_tui.py).
_HOME = os.environ.get("SUDO_HOME") or os.path.expanduser("~")
CONFIG_DIR = os.path.join(_HOME, ".config", "gpu-tui")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")
STATUS_MARKER = "/tmp/gpu_oc_active"

# Fallback values, kept in sync with the 'default' profile.
DEFAULTS = {
    "power_w": 260,
    "clock_min": 210,
    "clock_max": 2730,
    "mem_off": 400,
    "gfx_off": 200,
}
FIELDS = tuple(DEFAULTS)


def load_profiles():
    """Return (profiles, selected) from profiles.json; ({}, '') if absent."""
    try:
        with open(PROFILES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, ""
    if not isinstance(data, dict):
        return {}, ""
    selected = data.pop("selected", "")
    profiles = {k: v for k, v in data.items() if isinstance(v, dict)}
    return profiles, selected


def save_profiles(profiles, selected):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PROFILES_FILE, "w") as f:
        json.dump({"selected": selected, **profiles}, f, indent=2)


def pick_profile(name_arg=""):
    """Return (profile_name, profile_dict) from the .json values."""
    profiles, selected = load_profiles()
    name = name_arg or selected
    if not name:
        name = "default" if "default" in profiles else (next(iter(profiles), "") if profiles else "")
    profile = profiles.get(name)
    if profile is None and not profiles:
        return "default", dict(DEFAULTS)
    if profile is None:
        sys.exit(f"[overclock] profile '{name_arg or selected}' not found in {PROFILES_FILE}")
    return (name or "default"), profile


def apply(profile):
    missing = [k for k in FIELDS if k not in profile]
    if missing:
        sys.exit(f"[overclock] profile is missing keys: {', '.join(missing)}")
    nvmlInit()
    try:
        myGPU = nvmlDeviceGetHandleByIndex(0)
        nvmlDeviceSetPowerManagementLimit(myGPU, int(profile["power_w"]) * 1000)
        nvmlDeviceSetGpuLockedClocks(myGPU, int(profile["clock_min"]),
                                     int(profile["clock_max"]))

        infoMemP0 = c_nvmlClockOffset_t()
        infoMemP0.version = nvmlClockOffset_v1
        infoMemP0.type = NVML_CLOCK_MEM
        infoMemP0.pstate = NVML_PSTATE_0
        # mem offset is stored x2 by the driver
        infoMemP0.clockOffsetMHz = int(profile["mem_off"]) * 2
        nvmlDeviceSetClockOffsets(myGPU, byref(infoMemP0))

        infoGraphicsP0 = c_nvmlClockOffset_t()
        infoGraphicsP0.version = nvmlClockOffset_v1
        infoGraphicsP0.type = NVML_CLOCK_GRAPHICS
        infoGraphicsP0.pstate = NVML_PSTATE_0
        infoGraphicsP0.clockOffsetMHz = int(profile["gfx_off"])
        nvmlDeviceSetClockOffsets(myGPU, byref(infoGraphicsP0))
    finally:
        nvmlShutdown()


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--list", "-l"):
        profiles, selected = load_profiles()
        if not profiles:
            print(f"(no profiles in {PROFILES_FILE})")
            return
        for name in profiles:
            mark = "*" if name == selected else " "
            print(f"{mark} {name}")
        return

    name_arg = args[0] if args and not args[0].startswith("-") else ""
    name, profile = pick_profile(name_arg)
    apply(profile)
    if name_arg:
        profiles, _ = load_profiles()
        save_profiles(profiles, name)
    # Flag file: other tools can tell an OC is active (removed by reset_overclock.py).
    with open(STATUS_MARKER, "w") as f:
        f.write(name)
    print(f"[overclock] applied '{name}'")


if __name__ == "__main__":
    main()
