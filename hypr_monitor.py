#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 alystor007
"""
hypr_monitor — Hyprland display state for the TUI.

Reads `hyprctl monitors -j` and applies display changes live via
`hyprctl keyword monitor <line>`. Persisted state lives in a dedicated
config file the user sources from their main config once:

    source = ~/.config/hypr/monitors-gpu-tui.conf

Everything is stdlib only: json, os, subprocess, shutil.
"""

import json
import os
import re
import shutil
import subprocess

# Under sudo, $HOME is /root — use the invoking user's home (SUDO_HOME)
# so the config path is the same with and without sudo.
_HOME = os.environ.get("SUDO_HOME") or os.path.expanduser("~")
HYPR_DIR = os.path.join(_HOME, ".config", "hypr")
# Generated file — the user sources it from monitors.conf once; the tool
# owns it entirely and never touches the hand-maintained config.
DISPLAY_CONF = os.path.join(HYPR_DIR, "monitors-gpu-tui.conf")

COLOR_MODES = ("srgb", "wide", "hdr")
CM_VALUES = ("srgb", "wide", "hdr", "hdredid", "auto")

# VRR modes (the `vrr, <N>` value on a `monitor =` line):
# 0 = off, 1 = on, 2 = fullscreen only,
# 3 = fullscreen with video or game content type.
VRR_MODES = (0, 1, 2, 3)
VRR_LABELS = ("off", "on", "fullscreen only",
              "fullscreen with video or game content type")
VRR_SHORT = ("off", "on", "fs-only", "fs-video/game")


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hz(r) -> str:
    """Refresh rate as the config wants it: 60.00 -> '60', 144.02 -> '144.02'."""
    r = _f(r)
    return str(int(r)) if r == int(r) else f"{r:g}"


def hyprctl_available() -> bool:
    return shutil.which("hyprctl") is not None


def _caller_sig(uid: int) -> str:
    """HYPRLAND_INSTANCE_SIGNATURE from the caller's own process envs
    (/proc — readable as root, which is when this is needed)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                status = f.read()
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(uid):
                    try:
                        with open(f"/proc/{pid}/environ") as e:
                            for tok in e.read().split("\0"):
                                if tok.startswith("HYPRLAND_INSTANCE_SIGNATURE="):
                                    return tok.split("=", 1)[1]
                    except OSError:
                        pass
                break
    return ""


def _hypr_env(xdg_base: str = "/run/user") -> dict:
    """Extra env for reaching the caller's Hyprland when the TUI runs as
    root (sudo).

    sudo's env_reset strips the session variables, so a root hyprctl
    cannot find Hyprland's socket and the Display section would vanish.
    Restore them from the caller's uid (SUDO_UID): XDG_RUNTIME_DIR is
    /run/user/<uid>; the instance signature is the name of the hypr
    socket dir, or, when several sessions exist, from the caller's
    process envs. {} when not running as root (env stays untouched)."""
    if os.geteuid() != 0:
        return {}
    uid = os.environ.get("SUDO_UID", "")
    if not uid.isdigit():
        # Direct root run (no sudo): fall back to the sole login session.
        try:
            cands = [d for d in os.listdir(xdg_base) if d.isdigit()]
        except OSError:
            return {}
        if len(cands) != 1:
            return {}
        uid = cands[0]
    xrt = os.path.join(xdg_base, uid)
    if not os.path.isdir(xrt):
        return {}
    env = {"XDG_RUNTIME_DIR": xrt}
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not sig:
        hypr = os.path.join(xrt, "hypr")
        try:
            sigs = [d for d in os.listdir(hypr)
                    if os.path.exists(os.path.join(hypr, d, ".socket.sock"))
                    or os.path.exists(os.path.join(hypr, d, "agent.sock"))]
        except OSError:
            sigs = []
        if len(sigs) == 1:
            sig = sigs[0]
        elif len(sigs) > 1:
            # several live sessions — pick the caller's own (the one whose
            # signature matches the caller's envs), else none (never guess)
            want = _caller_sig(int(uid))
            sig = want if want in sigs else ""
    if sig:
        env["HYPRLAND_INSTANCE_SIGNATURE"] = sig
    return env


def hyprctl_raw(*args, timeout=5) -> subprocess.CompletedProcess | None:
    # _hypr_env() restores the caller's Hyprland session vars when the
    # TUI runs under sudo (empty dict otherwise — env stays as-is).
    env = {**os.environ, **_hypr_env()}
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_mode(m) -> dict:
    """One entry of a monitor's modes list."""
    return {
        "w": int(_f(m.get("width"))),
        "h": int(_f(m.get("height"))),
        "hz": _f(m.get("refresh_rate")),
        "vrr": bool(m.get("vrr", False)),
    }


_MODE_RE = re.compile(r"^(\d+)x(\d+)@([\d.]+)")


def _parse_avail(s) -> dict | None:
    """One `availableModes` string: '3440x1440@164.90Hz' -> mode dict.

    The string format carries no per-mode vrr flag, so the vrr key is
    omitted (unknown) rather than assumed off."""
    m = _MODE_RE.match(str(s or ""))
    if not m:
        return None
    return {"w": int(m.group(1)), "h": int(m.group(2)),
            "hz": _f(m.group(3))}


def parse_monitors(data) -> list:
    """`hyprctl monitors -j` payload -> clean per-monitor dicts.

    Hyprland changed field names between releases (max_refresh vs
    maxRefresh, refreshRate, vrr_enabled vs vrr, ...), so every read
    tolerates both spellings and unknown values. The mode table comes
    from the structured `modes` list when present, else from the
    `availableModes` string list; the active rate from `active_mode`
    or the top-level refresh-rate field.
    """
    mons = []
    if isinstance(data, dict):
        data = data.get("monitors", [])
    if not isinstance(data, list):
        return mons
    for m in data:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        if not name:
            continue
        modes = []
        raw_modes = m.get("modes")
        if isinstance(raw_modes, list) and raw_modes:
            modes = [_parse_mode(x) for x in raw_modes if isinstance(x, dict)]
        if not modes:
            for s in (m.get("availableModes") or []):
                p = _parse_avail(s)
                if p:
                    modes.append(p)
        active = m.get("active_mode")
        if isinstance(active, dict):
            active = _parse_mode(active)
        else:
            hz = _f(m.get("refresh_rate"), _f(m.get("refreshRate")))
            if hz <= 0 and modes:
                hz = max(x["hz"] for x in modes)
            active = {"w": int(_f(m.get("width"))),
                      "h": int(_f(m.get("height"))),
                      "hz": hz,
                      "vrr": False}
        max_hz = _f(m.get("max_refresh"), _f(m.get("maxRefresh")))
        if max_hz <= 0 and modes:
            max_hz = max(x["hz"] for x in modes)
        enabled = m.get("enabled")
        if enabled is None:
            # enabled unless the monitor is explicitly disabled
            enabled = not m.get("disabled", False)
        _vs = m.get("vrr_supported")
        if _vs is None:
            _vs = m.get("vrr_enabled_supported")
        mons.append({
            "name": str(name),
            "make": str(m.get("make", "")),
            "model": str(m.get("model", "")),
            "active": active,
            "modes": modes,
            "max_hz": max_hz,
            "vrr": m.get("vrr", m.get("vrr_enabled", False)),
            # None when the field is absent (older hyprctl) — "unknown",
            # not "unsupported".
            "vrr_supported": bool(_vs) if _vs is not None else None,
            "eotf": str(m.get("eotf", m.get("cm", m.get("colorManagementPreset", ""))) or ""),
            "sdr_brightness": _f(m.get("sdr_brightness", m.get("sdrBrightness", 1.0)), 1.0),
            "sdr_saturation": _f(m.get("sdr_saturation", m.get("sdrSaturation", 1.0)), 1.0),
            "enabled": bool(enabled),
            "position": (int(_f(m.get("x"))), int(_f(m.get("y")))),
        })
    return mons


def get_monitors() -> list:
    """Query hyprctl. [] when hyprctl is missing or the output unparseable."""
    if not hyprctl_available():
        return []
    r = hyprctl_raw("monitors", "-j")
    if r is None or r.returncode != 0:
        return []
    try:
        return parse_monitors(json.loads(r.stdout))
    except (ValueError, TypeError):
        return []


def native_mode(m: dict) -> dict:
    """Highest-resolution mode; ties broken by refresh rate."""
    if not m.get("modes"):
        return m["active"]
    return max(m["modes"], key=lambda x: (x["w"], x["h"], x["hz"]))


def rates_at_native(m: dict) -> list:
    """Refresh rates available at the monitor's native resolution,
    highest first. Falls back to the active rate when the mode list is
    missing or has no native entry."""
    nat = native_mode(m)
    rates = sorted({round(x["hz"], 2) for x in m.get("modes", [])
                    if x["w"] == nat["w"] and x["h"] == nat["h"]}, reverse=True)
    if rates:
        return rates
    return [round(nat["hz"], 2)]


def vrr_available_at(m: dict, hz: float) -> bool | None:
    """True/False when the mode table says; None when unknown (no vrr
    flags in the mode list — old hyprctl or no VRR on the panel)."""
    nat = native_mode(m)
    for x in m.get("modes", []):
        if x["w"] == nat["w"] and x["h"] == nat["h"] and round(x["hz"], 2) == round(_f(hz), 2):
            if "vrr" in x:
                return bool(x["vrr"])
            return None
    return None


def vrr_supported(m: dict) -> bool | None:
    """True/False when hyprctl reports it; None when unknown (older
    hyprctl omits the flag, or the monitor JSON carries no
    vrr_supported — e.g. the availableModes-only shape)."""
    v = m.get("vrr_supported")
    if v is None:
        v = m.get("vrr_enabled_supported")
    if v is None:
        return None
    return bool(v)


def color_from_eotf(eotf: str) -> str:
    """Map a reported eotf/cm value onto the wizard's color modes.

    hyprctl reports eotf as SDR / PQ / HLG (PQ = HDR10), so PQ maps to
    hdr; cm-style values (wide, bt.2020, ...) map to wide; everything
    else to srgb.
    """
    e = (eotf or "").lower().replace(".", "")
    if "hdr" in e or e in ("pq", "hlg"):
        return "hdr"
    if "wide" in e or "bt2020" in e:
        return "wide"
    return "srgb"


def build_line(m: dict, *, enabled: bool = True, hz: float | None = None,
               color: str = "srgb", vrr: int | None = 0,
               bitdepth: int = 10,
               sdr_brightness: float = 1.0, sdr_saturation: float = 1.0) -> str:
    """A `monitor =` config line for one display.

    vrr is the VRR mode: 0 = off, 1 = on, 2 = fullscreen only, 3 =
    fullscreen with video/game content type; None = omit the token.
    sdrbrightness/sdrsaturation are emitted only for HDR (they are
    no-ops otherwise). bitdepth is the user's choice (8 or 10),
    emitted for HDR and wide — both need the link negotiated at the
    chosen depth — hyprctl reports no bit-depth fields at all (its
    JSON has no bitdepth/bit_depths; upstream issue #4971), so it
    can't be auto-detected. No luminance tokens — the classic monitor=
    parser rejects sdr_max_luminance/sdr_min_luminance.
    """
    if not enabled:
        return f"monitor = {m['name']}, disable"
    nat = native_mode(m)
    h = _f(hz) if hz else nat["hz"]
    pos = m.get("position", (0, 0))
    parts = [f"monitor = {m['name']}", f"{nat['w']}x{nat['h']}@{_hz(h)}",
             f"{pos[0]}x{pos[1]}", "1"]
    if vrr is not None:
        parts += ["vrr", str(int(vrr))]
    if color in CM_VALUES and color != "srgb":
        parts += ["cm", color]
    if color in ("hdr", "wide"):
        # Both HDR and wide color space need the link negotiated at the
        # chosen depth (Hyprland warns on wide without 10-bit), so the
        # token is emitted for either; sdrbrightness/sdrsaturation are
        # HDR-only below.
        bd = int(bitdepth)
        if bd in (8, 10):
            parts += ["bitdepth", str(bd)]
    if color == "hdr":
        b, s = round(float(sdr_brightness), 2), round(float(sdr_saturation), 2)
        if b != 1.0:
            parts += ["sdrbrightness", f"{b:.2f}"]
        if s != 1.0:
            parts += ["sdrsaturation", f"{s:.2f}"]
    return ", ".join(parts)


def apply_line(line: str) -> tuple[bool, str]:
    """`hyprctl keyword monitor <line>` — live, volatile, no reload."""
    r = hyprctl_raw("keyword", "monitor", line)
    if r is None:
        return False, "hyprctl not available"
    out = (r.stderr or r.stdout).strip()
    if r.returncode != 0:
        return False, out or f"hyprctl rc={r.returncode}"
    return True, out


def persist_file(path: str, lines: list) -> bool:
    """Write the generated config atomically (tmp + rename)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = ("# generated by nvidia-tui-overclocker — do not edit by hand;\n"
                   "# re-generated every time the display wizard saves\n\n")
        content += "\n".join(lines) + "\n"
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_display_state() -> dict:
    """Last-saved per-monitor display options (wizard defaults)."""
    try:
        with open(os.path.join(os.path.dirname(DISPLAY_CONF), "gpu-tui-display.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_display_state(state: dict) -> bool:
    path = os.path.join(os.path.dirname(DISPLAY_CONF), "gpu-tui-display.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    # Probe: dump the parsed state (run this on the real machine first —
    # the sandbox has no hyprctl, so field names are verified here).
    if not hyprctl_available():
        print("hyprctl not available")
    else:
        print(json.dumps(get_monitors(), indent=2))
