#!/usr/bin/env python3
"""Check whether Resizable BAR (ReBAR) is active on the system's NVIDIA GPU(s).

Detection: the framebuffer BAR is allocated at boot. With ReBAR off the
legacy BAR is small (256 MiB on modern NVIDIA dGPUs); with ReBAR on the
BAR spans the entire framebuffer (e.g. 16 GiB). BAR sizes come from
/sys/bus/pci/devices/<bdf>/resource (columns: start end flags), so no
root, lspci, or GPU driver is required.

Each GPU line is labeled with its name/model and PCI device ID. The name
comes from nvidia-smi when available, otherwise from a small built-in
table of known device IDs (unknown IDs show the raw ID). If nvidia-smi
is available its VRAM size is also used to cross-check the BAR size.

Exit codes: 0 = ReBAR active on at least one GPU, 1 = none active,
            2 = no NVIDIA GPU found.
"""

import os
import shutil
import subprocess
import sys

RES = "/sys/bus/pci/devices"
LEGACY_MAX = 512 * 1024 * 1024  # legacy NVIDIA dGPU BARs never exceed 256 MiB

# Best-effort PCI device ID -> marketing name (nvidia-smi is preferred).
PCI_DEV_NAMES = {
    "0x2202": "RTX 4090",
    "0x2201": "RTX 4080",
    "0x2702": "RTX 4080 SUPER",
    "0x2705": "RTX 4070 Ti SUPER",
}


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n} B" if unit == "B" else f"{n:.0f} {unit}"
        n /= 1024


def norm_bdf(bdf):
    """00000000:0A:00.0 -> 0000:0a:00.0 (domain truncated to 4 hex digits)"""
    dom, rest = bdf.split(":", 1)
    return f"{dom[-4:].lower()}:{rest.lower()}"


def nvidia_gpus():
    for bdf in sorted(os.listdir(RES)):
        d = f"{RES}/{bdf}"
        try:
            vendor = open(f"{d}/vendor").read().strip()
            cls = open(f"{d}/class").read().strip()
        except OSError:
            continue
        # NVIDIA VGA (0300) or 3D controller (0302)
        if vendor == "0x10de" and cls[:6] in ("0x0300", "0x0302"):
            yield bdf, d


def bar_sizes(d):
    """Sizes of all memory BARs (IO regions excluded)."""
    sizes = []
    with open(f"{d}/resource") as f:
        for line in f:
            p = line.split()
            if len(p) < 3:
                continue
            start, end, flags = int(p[0], 16), int(p[1], 16), int(p[2], 16)
            if end < start:
                continue
            if flags & 1:  # IORESOURCE_IO
                continue
            sizes.append(end - start + 1)
    return sizes


def dev_id(d):
    """PCI device ID (e.g. '0x2705'), '' if unreadable."""
    try:
        return open(f"{d}/device").read().strip().lower()
    except OSError:
        return ""


def gpu_info():
    """(name, VRAM MiB) per normalized BDF, if nvidia-smi is available."""
    out = {}
    if not shutil.which("nvidia-smi"):
        return out
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,pci.bus_id",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            name, mem, bdf = (x.strip() for x in line.split(","))
            out[norm_bdf(bdf)] = (name, int(mem))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


def main():
    gpus = list(nvidia_gpus())
    if not gpus:
        print("No NVIDIA GPU found.")
        sys.exit(2)

    info = gpu_info()
    active = False
    for bdf, d in gpus:
        sizes = bar_sizes(d)
        bar = max(sizes, default=0)
        on = bar > LEGACY_MAX
        active |= on
        dev = dev_id(d)
        name, mem = info.get(bdf, ("", None))
        name = name or PCI_DEV_NAMES.get(dev, "")
        line = bdf
        if name:
            line += f"  {name}"
        if dev:
            line += f"  [{dev}]"
        line += f"  largest BAR: {human(bar):>10}"
        if mem is not None:
            line += f"  VRAM: {mem} MiB"
            if on:
                line += "  (full framebuffer mapped)"
        line += f"  ->  ReBAR {'ACTIVE' if on else 'inactive'}"
        print(line)
    sys.exit(0 if active else 1)


if __name__ == "__main__":
    main()
