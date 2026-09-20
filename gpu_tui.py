#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 alystor007
"""
gpu-tui v3 — a lazydocker-style TUI for GPU overclocking.

Keys:
  1 or a  -> Activate overclock (selected profile)
  2 or d  -> Deactivate overclock
  n       -> Define and save a new OC profile
  x       -> Select which profile activation uses
  f       -> Set fan speed (manual %, or 'auto' to revert) — needs sudo
  t       -> Cycle theme (saved between runs)
  e       -> Edit the highlighted monitor (wizard: rate/VRR/color/SDR)
             The Hyprland Display section is always visible and needs no
             sudo (hyprctl talks to the user's compositor session).
  q       -> Quit
  ?       -> Help overlay

Profiles live in ~/.config/gpu-tui/profiles.json — the single file the OC
scripts read. /tmp/gpu_oc_active marks that an OC is currently applied.
Display options persist to ~/.config/hypr/monitors-gpu-tui.conf (a file
you source from monitors.conf once — see the hypr_monitor module).
"""

import curses
import json
import os
import re
import subprocess
import sys
import time

try:
    import hypr_monitor   # Hyprland display state (stdlib only)
except ImportError:
    hypr_monitor = None

__version__ = "1.2.0"   # bump in the same commit as the git tag

# ============================================================
#  SCRIPTS
# ============================================================

# Scripts live next to this TUI — move them together, no config needed.
_HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVATE_SCRIPT = os.path.join(_HERE, "apply_overclock.py")
DEACTIVATE_SCRIPT = os.path.join(_HERE, "reset_overclock.py")
STATUS_MARKER = "/tmp/gpu_oc_active"   # apply_overclock.py writes the active profile name here
REBAR_SCRIPT = os.path.join(_HERE, "rebar_check.py")
FAN_SCRIPT = os.path.join(_HERE, "fan_control.py")

# ============================================================
#  OC PROFILES
# ============================================================
# Single source of truth: profiles.json — no temp files.
# Shape: { "selected": "default", "default": {...}, ... }

# Under sudo, $HOME is /root — use the invoking user's home (SUDO_HOME)
# so the config path is the same with and without sudo.
_HOME = os.environ.get("SUDO_HOME") or os.path.expanduser("~")
CONFIG_DIR = os.path.join(_HOME, ".config", "gpu-tui")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")

PROFILE_FIELDS = [
    ("power_w",   "Power (W)",         "260"),
    ("clock_min", "Clock min (MHz)",   "210"),
    ("clock_max", "Clock max (MHz)",   "2730"),
    ("mem_off",   "Mem offset (MHz)",  "400"),
    ("gfx_off",   "GFX offset (MHz)",  "200"),
]

# The first seed of the default profile used 0/0 offsets by mistake —
# migrate that exact stale value to the real defaults if untouched.
_BAD_SEED = {"power_w": 260, "clock_min": 210, "clock_max": 2730,
            "mem_off": 0, "gfx_off": 0}


def load_state() -> tuple[dict, str]:
    """Read profiles.json. Returns (profiles, selected_name)."""
    try:
        with open(PROFILES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    selected = data.pop("selected", "")
    profiles = {k: v for k, v in data.items() if isinstance(v, dict)}
    return profiles, selected


def save_profiles(profiles: dict, selected: str) -> bool:
    """Write profiles.json — the selected name lives inside it (no extra files).

    Returns True on success, False if the write could not be made (a failed
    write must never be silent: it would otherwise drop the profile from
    disk while it still shows in the TUI).
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {"selected": selected}
        data.update(profiles)
        with open(PROFILES_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def profile_summary(v: dict) -> str:
    """Compact one-line summary, e.g.
    '260W 210-2730MHz Mem+400 GFX+200' — short enough to fit on one row."""
    return (f"{v['power_w']}W {v['clock_min']}-{v['clock_max']}MHz "
            f"Mem+{v['mem_off']} GFX+{v['gfx_off']}")


# ============================================================
#  THEMES
# ============================================================
# Pair 1 = ok/active, 2 = err/inactive, 3 = header, 4 = body.
# 256-color themes fall back to "dark" on terminals without 256 colors.

# "btn" = accent for the option buttons ([1], [2], [n], ...) in the hint
# line, so the keys read as pressable buttons. Pair 6.
THEMES = {
    "dark":      {"fg": [curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_WHITE], "bg": curses.COLOR_BLACK, "btn": curses.COLOR_YELLOW},
    "light":     {"fg": [curses.COLOR_BLUE,  curses.COLOR_RED, curses.COLOR_MAGENTA, curses.COLOR_BLACK], "bg": curses.COLOR_WHITE, "btn": curses.COLOR_BLUE},
    "matrix":    {"fg": [curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN, curses.COLOR_GREEN], "bg": curses.COLOR_BLACK, "btn": curses.COLOR_GREEN},
    "solarized": {"fg": [64, 166, 172, 252],  "bg": 235, "btn": 178},
    "gruvbox":   {"fg": [142, 167, 215, 223], "bg": 235, "btn": 216},
    "nord":      {"fg": [108, 173, 179, 252], "bg": 235, "btn": 117},
    "dracula":   {"fg": [46, 203, 190, 252],  "bg": 235, "btn": 226},
    # omarchy.org palette (Tokyo Night base + Omarchy green accent).
    # "transparent": -1 = the terminal's actual default fg/bg (needs
    # use_default_colors()), no bkgd fill — terminal shows through.
    "transparent": {"fg": [114, 124, 146, 111], "bg": -1, "btn": 118, "transparent": True},
}

THEME_FILE = os.path.join(CONFIG_DIR, "theme")


def next_theme(current: str) -> str:
    names = list(THEMES)
    return names[(names.index(current) + 1) % len(names)]


def apply_theme(stdscr, name: str) -> str:
    """Init color pairs for `name` and set the window background;
    returns the theme actually applied."""
    t = THEMES.get(name, THEMES["dark"])
    if max([*t["fg"], t["bg"], t["btn"]]) >= curses.COLORS:
        t, name = THEMES["dark"], "dark"
    if t.get("transparent"):
        # -1 only means "terminal default" once this flag is set.
        curses.use_default_colors()
    for i, fg in enumerate(t["fg"]):
        curses.init_pair(i + 1, fg, t["bg"])
    if t.get("transparent"):
        # No background: pair 5 = the terminal's own default colors,
        # and we skip bkgd() so its background shows through.
        curses.init_pair(5, -1, -1)
        curses.init_pair(6, t["btn"], -1)
        return name
    # Pair 5 = window background: whole-screen bg follows the theme.
    # (Pair 0 is the terminal default and cannot be re-init'd on
    # Python >= 3.14 / ncurses 6.5+, hence a dedicated pair.)
    curses.init_pair(5, t["fg"][3], t["bg"])
    # Pair 6 = option-button accent (the [keys] in the hint line).
    curses.init_pair(6, t["btn"], t["bg"])
    stdscr.bkgd(" ", curses.color_pair(5))
    return name


def draw_hint(stdscr, row, col, text, width):
    """Render a hint line with bracketed keys ([1], [n], ...) in the
    theme's button accent, so they read as pressable buttons."""
    key_attr = curses.color_pair(6) | curses.A_BOLD
    c = col
    for seg in re.split(r"(\[[^\]]+\])", text):
        if not seg:
            continue
        if c >= width:          # nothing left on this row: stop cleanly
            break
        attr = key_attr if seg.startswith("[") else curses.color_pair(4)
        stdscr.addnstr(row, c, seg, width - c, attr)
        c += len(seg)


def load_theme() -> str:
    try:
        with open(THEME_FILE) as f:
            name = f.read().strip()
    except OSError:
        return "dark"
    return name if name in THEMES else "dark"


def save_theme(name: str) -> None:
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w") as f:
            f.write(name)
    except OSError:
        pass


# ============================================================
#  ACTIONS
# ============================================================

def activate_overclock() -> tuple[int, str]:
    """apply_overclock.py loads the selected profile from profiles.json itself."""
    r = subprocess.run(
        ["python3", ACTIVATE_SCRIPT],
        capture_output=True, text=True,
    )
    return r.returncode, (r.stderr or r.stdout).strip()


def deactivate_overclock() -> tuple[int, str]:
    r = subprocess.run(
        ["python3", DEACTIVATE_SCRIPT],
        capture_output=True, text=True,
    )
    return r.returncode, (r.stderr or r.stdout).strip()


def get_status() -> tuple[bool, str]:
    """Return (active, active_profile_name) from the flag file."""
    try:
        with open(STATUS_MARKER) as f:
            return True, f.read().strip()
    except OSError:
        return False, ""


def get_gpu_stats() -> dict:
    """Query nvidia-smi. Returns dict; empty dict on failure."""
    fields = ["clocks.sm", "clocks.max.sm", "fan.speed", "temperature.gpu",
              "power.draw", "power.limit", "utilization.gpu",
              "memory.used", "memory.total"]
    fmt = ",".join(fields)
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={fmt}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return {}
        vals = [v.strip() for v in r.stdout.split(",")]
        return {
            "clock":     vals[0],   # MHz
            "clock_max": vals[1],   # MHz (max graphics clock)
            "fan":       vals[2],   # %
            "temp":      vals[3],   # C
            "power":     vals[4],   # W (current draw)
            "power_max": vals[5],   # W (max/limit)
            "util":      vals[6],   # %
            "mem":       vals[7],   # MiB
            "mem_total": vals[8],   # MiB
        }
    except Exception:
        return {}


def get_gpu_processes():
    """Top VRAM-consuming processes, from nvidia-smi's compute-apps query.

    A separate call from get_gpu_stats(): nvidia-smi accepts only one
    --query-* switch per invocation, so the process list is its own
    subprocess on the same telemetry cadence. Returns a list of
    (name, mib) tuples sorted by VRAM usage descending, capped at the top 6;
    [] on any failure (missing binary, non-zero exit, unparseable rows).
    """
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    procs = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("pid,"):
            continue
        # csv row: pid, process_name, used_gpu_memory. Split on the FIRST
        # comma (pid) and the LAST comma (memory) so names containing commas
        # survive; the driver reports "N/A" for names it can't map.
        first = line.index(",") if "," in line else -1
        last = line.rfind(",")
        if first == -1 or last == first:
            continue
        pid = line[:first].strip()
        name = line[first + 1:last].strip()
        # nvidia-smi reports the full command line (a long path + args);
        # show just the executable's base name so the 15-char column is
        # readable ("chromium", not "/usr/lib/chromium/chromium").
        if name and name not in ("N/A", "[N/A]"):
            name = name.split()[0].rsplit("/", 1)[-1]
        mem = line[last + 1:].strip()
        # Bare integer with nounits; tolerate a unit suffix ("96 MiB") so a
        # driver/format change can never silently drop every row.
        first_token = mem.split()[0] if mem.split() else ""
        digits = "".join(c for c in first_token if c.isdigit())
        if not digits:
            continue
        mem_v = int(digits)
        if name in ("", "N/A", "[N/A]"):
            name = f"pid {pid}" if pid else "unknown"
        procs.append((name, mem_v))
    procs.sort(key=lambda p: p[1], reverse=True)
    return procs[:6]


def _f(v) -> float:
    """float(v); 0.0 on missing/garbage (nvidia-smi 'N/A', empty)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _trim0(s: str) -> str:
    """260.00 -> 260, 262.50 -> 262.5; non-numeric strings pass through."""
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else f"{f:g}"


def _bar(value: float, limit: float, width: int = 12) -> str:
    """A ▓/░ fill string for a bar meter. Empty when there is no usable
    limit, so the value column stays aligned across rows."""
    if limit <= 0 or value < 0:
        return ""
    filled = min(width, max(0, int(round(value / limit * width))))
    return "▓" * filled + "░" * (width - filled)


def _lerp_field(prev: dict, cur: dict, key: str, frac: float) -> float:
    """A telemetry field interpolated between the previous and current
    sample; the current value when there is no previous one."""
    c = _f(cur.get(key, ""))
    if not prev:
        return c
    p = _f(prev.get(key, ""))
    return p + (c - p) * frac


def _fmt_rebar(active: bool, bdf: str, name: str, bar: str, vram: str) -> str:
    """One compact line; VRAM is dropped if the line would not fit an
    80-col terminal (the value starts at PAD+8)."""
    core = " ".join(p for p in ("ACTIVE" if active else "inactive", bdf, name) if p)
    tail = f" BAR {bar}" if bar else ""
    full = core + tail + (f", VRAM {vram} MiB" if vram else "")
    return full if len(full) <= 66 else core + tail


def get_rebar():
    """ReBAR status from rebar_check.py. The BAR size is fixed at boot, so
    this is queried once at startup — not on the telemetry cadence.
    Returns a list of (text, active) rows, one per GPU."""
    try:
        r = subprocess.run(["python3", REBAR_SCRIPT],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return [("unknown (rebar_check.py could not be run)", False)]
    rows = []
    for line in r.stdout.splitlines():
        if "ReBAR " not in line:
            continue
        active = line.rstrip().endswith("ReBAR ACTIVE")
        bdf = line.split()[0]
        mid = re.split(r"\[0x[0-9a-f]+\]\s+", line, 1)[0]
        name = re.sub(r"^NVIDIA GeForce ", "", re.sub(r"^\S+\s*", "", mid).strip())
        bar = re.search(r"largest BAR:\s+(\S+ \S+)", line)
        vram = re.search(r"VRAM:\s+(\d+) MiB", line)
        rows.append((_fmt_rebar(active, bdf, name,
                                bar.group(1) if bar else "",
                                vram.group(1) if vram else ""), active))
    if not rows:
        if "No NVIDIA GPU found" in r.stdout:
            rows = [("no NVIDIA GPU found", False)]
        else:
            # missing script, python error, or unexpected output
            rows = [("unknown (rebar_check.py failed)", False)]
    return rows


def prepend_log(log: str, entry: str) -> str:
    """Prepend an entry, keeping one line per entry."""
    return entry if not log else entry + "\n" + log


def get_fan_state() -> dict:
    """Fan mode/speed from fan_control.py status (JSON on stdout).

    Read-only, so it runs on the 2s telemetry cadence like nvidia-smi.
    Returns {} when the script is missing, pynvml is absent, or the GPU is
    unavailable — the TUI then just shows the plain fan % (no mode tag).
    """
    try:
        r = subprocess.run([sys.executable, FAN_SCRIPT, "status"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def set_fan_manual(pct: int) -> tuple[int, str]:
    """fan_control.py set <PCT> — all fans to a fixed speed (needs root)."""
    r = subprocess.run([sys.executable, FAN_SCRIPT, "set", str(pct)],
                       capture_output=True, text=True)
    return r.returncode, (r.stderr or r.stdout).strip()


def set_fan_auto() -> tuple[int, str]:
    """fan_control.py auto — all fans back to the driver's auto curve."""
    r = subprocess.run([sys.executable, FAN_SCRIPT, "auto"],
                       capture_output=True, text=True)
    return r.returncode, (r.stderr or r.stdout).strip()


# ============================================================
#  PROFILE WIZARDS
# ============================================================

def prompt(stdscr, sh: int, sw: int, label: str, default: str = "") -> str | None:
    """Blocking text prompt on the footer row. None on Esc."""
    stdscr.timeout(-1)
    curses.echo()
    stdscr.move(sh - 1, 0)
    stdscr.clrtoeol()
    stdscr.addnstr(sh - 1, 0, f"{label} [{default}]: ", sw)
    stdscr.refresh()
    res = stdscr.getstr()
    curses.noecho()
    stdscr.timeout(100)
    raw = res[1] if isinstance(res, tuple) else res  # ncurses returns (n, b) or bare bytes
    text = raw.decode(errors="replace").strip()
    if text.startswith("\x1b"):   # Esc then Enter = cancel
        return None
    return text if text else default


def new_profile(stdscr, sh: int, sw: int, profiles: dict) -> tuple[str, str]:
    """Define and save a new profile. Returns (selected_name, log_msg)."""
    name = prompt(stdscr, sh, sw, "Profile name", "custom")
    if not name:
        return "", "[profile] cancelled"
    if name == "selected":
        return "", "[profile] name 'selected' is reserved"
    if name in profiles:
        return "", f"[profile] '{name}' already exists"
    values = {}
    for key, label, dflt in PROFILE_FIELDS:
        v = prompt(stdscr, sh, sw, label, dflt)
        if v is None:
            return "", "[profile] cancelled"
        try:
            values[key] = int(v)
        except ValueError:
            return "", f"[profile] invalid number for {label}"
    profiles[name] = values
    return name, f"[profile] saved '{name}'"


# ============================================================
#  DISPLAY  —  Hyprland monitors (hyprctl)
# ============================================================
# One row per monitor under ReBAR, edited through the display wizard
# (jumpstart-style centered field box). Live changes go via
# `hyprctl keyword monitor`; saves also rewrite the generated config
# file (sourced from the user's monitors.conf). The section is always
# visible when hyprctl is available; state is cached and re-queried on
# demand (e / after a save), never on the 2s telemetry cadence.

DISPLAY_MS = 15000   # refresh the display cache at most every 15s


def _read_key(stdscr, restore_ms=100):
    """Read one key, decoding raw arrow sequences so arrows work with or
    without keypad(). Returns a curses code (-1 timeout, 27 bare Esc) or
    "up"/"down"/"left"/"right". A split sequence (27, '[' then timeout)
    is dropped, never treated as Esc."""
    c = stdscr.getch()
    if c != 27:
        return c
    # bare-Esc peek: 10ms (jumpstart-tui's value) — long enough for the
    # '[' of a real arrow/fn-key sequence, short enough that Esc feels
    # instant on top of curses.set_escdelay(25)
    stdscr.timeout(10)
    c2 = stdscr.getch()
    stdscr.timeout(restore_ms)
    if c2 not in (ord("["), ord("O")):
        return 27
    c3 = stdscr.getch()
    stdscr.timeout(restore_ms)
    if c3 == ord("A"):
        return "up"
    if c3 == ord("B"):
        return "down"
    if c3 == ord("C"):
        return "right"
    if c3 == ord("D"):
        return "left"
    if c3 == -1:
        return -1
    return 27


def _display_ok() -> bool:
    return hypr_monitor is not None and hypr_monitor.hyprctl_available()


def _display_init():
    """(rows, selected) for the Display section: ([], 0) when Hyprland
    is not running, else one row per monitor (disabled ones included)."""
    if not _display_ok():
        return [], 0
    mons = hypr_monitor.get_monitors()
    if not mons:
        return [], 0
    state = hypr_monitor.load_display_state()
    for m in mons:
        m["opts"] = state.get(m["name"]) or _display_default(m)
    return mons, 0


def _display_default(m: dict) -> dict:
    """Wizard defaults from the live state: native res, the current color
    mode, the current VRR mode (off when the panel reports no VRR
    support), current SDR values, and the monitor enabled."""
    nat = hypr_monitor.native_mode(m)
    hz = round(nat["hz"], 2)
    # hyprctl's per-monitor vrr flag is live state, not config (it reads
    # false unless VRR is actively engaged), so it can't seed the
    # default — enable VRR (mode 1) unless the panel explicitly reports
    # no support.
    vrr = 0 if hypr_monitor.vrr_supported(m) is False else 1
    return {"hz": hz,
            "color": hypr_monitor.color_from_eotf(m.get("eotf", "")),
            "vrr": vrr,
            "bd": 10,
            "sdr_b": round(float(m.get("sdr_brightness", 1.0)), 2),
            "sdr_s": round(float(m.get("sdr_saturation", 1.0)), 2),
            "enabled": bool(m.get("enabled", True))}


def _display_refresh(state, selected, now) -> tuple[list, int]:
    """Re-query hyprctl; keep the selected monitor by name (a hotplug
    re-indexes the list) and carry the edited opts forward by name."""
    if not _display_ok():
        return [], 0
    mons = hypr_monitor.get_monitors()
    if not mons:
        return [], 0
    old = {m["name"]: m.get("opts") for m in state}
    for m in mons:
        m["opts"] = old.get(m["name"]) or _display_default(m)
    sel_name = state[selected]["name"] if 0 <= selected < len(state) else None
    selected = 0
    for i, m in enumerate(mons):
        if m["name"] == sel_name:
            selected = i
            break
    return mons, selected


def _display_summary(m: dict) -> str:
    """Compact per-monitor row: name  WxH@HZ  [HDR|wide|sRGB][10-bit]
    [sdr b/s]  [VRR <mode>]  (off)."""
    o = m["opts"]
    if not o.get("enabled", True):
        return f"{m['name']}  off"
    a = m["active"]
    hz = o.get("hz") or a["hz"]
    col = o.get("color", "srgb")
    colw = "HDR" if col == "hdr" else ("wide" if col == "wide" else "sRGB")
    bd = f" {int(o.get('bd', 10))}-bit" if col in ("hdr", "wide") else ""
    sdr = ""
    if col == "hdr":
        sdr = f" sdr {o.get('sdr_b', 1.0):.2f}/{o.get('sdr_s', 1.0):.2f}"
    vrr = "VRR " + hypr_monitor.VRR_SHORT[min(3, max(0, int(o.get("vrr", 0))))]
    return f"{m['name']}  {a['w']}x{a['h']}@{hypr_monitor._hz(hz)}  {colw}{bd}{sdr}  {vrr}"


def _display_line(m: dict) -> str:
    """The `monitor =` line for one monitor's current opts. The vrr mode
    is emitted as-is (0..3); a panel that reports no VRR support is
    forced to 0."""
    o = m["opts"]
    vrr = min(3, max(0, int(o.get("vrr", 0))))
    if hypr_monitor.vrr_supported(m) is False:
        vrr = 0
    return hypr_monitor.build_line(
        m, enabled=o.get("enabled", True), hz=o.get("hz"),
        color=o.get("color", "srgb"), vrr=vrr,
        bitdepth=int(o.get("bd", 10)),
        sdr_brightness=o.get("sdr_b", 1.0), sdr_saturation=o.get("sdr_s", 1.0))


# --- display wizard (port of the jumpstart field-box wizard) ---

_DISPLAY_ALL_FIELDS = (
    ("Refresh (Hz)", "hz"),
    ("VRR", "vrr"),
    ("Color", "color"),
    ("Bit depth", "bd"),
    ("SDR brightness", "sdr_b"),
    ("SDR saturation", "sdr_s"),
    ("Enabled", "enabled"),
)

# Action hint above the bottom border — also the widest content in the
# box, so it drives the minimum width (see display_wizard).
_WIZARD_HINT = "↑↓ move · ←→ adjust · Enter save · Esc cancel"


def _display_fields(mon: dict, multi: bool) -> list:
    """(label, key, active) rows — always the full set, so hidden
    options stay visible (dimmed). A field is active only when it
    applies in the current state: Bit depth needs color=hdr|wide,
    SDR rows need color=hdr, Enabled needs 2+ monitors, everything
    else needs the monitor enabled (a disabled monitor shows just
    its active Enabled row)."""
    o = mon["opts"]
    on = o.get("enabled", True)
    out = []
    for label, key in _DISPLAY_ALL_FIELDS:
        col = o.get("color", "srgb")
        if key == "bd":
            en = on and col in ("hdr", "wide")
        elif key in ("sdr_b", "sdr_s"):
            en = on and col == "hdr"
        elif key == "enabled":
            en = multi
        else:
            en = on
        out.append((label, key, bool(en)))
    return out


def _display_field_value(mon: dict, key: str) -> str:
    o = mon["opts"]
    if key == "hz":
        txt = hypr_monitor._hz(o.get("hz", 0))
        if hypr_monitor.vrr_available_at(mon, o.get("hz", 0)) is False:
            txt += "*"
        return txt
    if key == "vrr":
        return hypr_monitor.VRR_SHORT[min(3, max(0, int(o.get("vrr", 0))))]
    if key == "color":
        return o.get("color", "srgb")
    if key == "bd":
        return f"{int(o.get('bd', 10))}-bit"
    if key == "sdr_b":
        return f"{o.get('sdr_b', 1.0):.2f}"
    if key == "sdr_s":
        return f"{o.get('sdr_s', 1.0):.2f}"
    return "on" if o.get("enabled", True) else "off"


def _display_error(mon: dict) -> str:
    """Error line: the selected rate lacks VRR while a VRR mode is
    requested, or the panel reports no VRR support at all."""
    o = mon["opts"]
    vrr = min(3, max(0, int(o.get("vrr", 0))))
    if vrr > 0 and hypr_monitor.vrr_supported(mon) is False:
        return "this monitor reports no VRR support"
    if vrr > 0 and hypr_monitor.vrr_available_at(mon, o.get("hz", 0)) is False:
        return f"no VRR at {hypr_monitor._hz(o.get('hz', 0))} Hz"
    return ""


def _display_draw(stdscr, sh, sw, top, left, bw, bh, title, mon, fields,
                  active_i, error):
    """Wizard box: border, title, one row per field (labels right-justified
    into a shared column, a fixed " : " separator, values left-justified;
    the active row gets a full-width highlight band behind the text),
    the action hint one row above the bottom border, and the error line
    over the bottom border (red)."""
    H, W = stdscr.getmaxyx()
    attr = curses.color_pair(4)
    for r in range(top, top + bh):
        if r < H - 1:
            stdscr.addnstr(r, left, " " * bw, bw)
    for r in range(top, top + bh):
        if r >= H - 1:
            break
        stdscr.addch(r, left, "┌" if r == top else ("└" if r == top + bh - 1 else "│"))
        stdscr.addch(r, left + bw - 1, "┐" if r == top else ("┘" if r == top + bh - 1 else "│"))
    stdscr.addnstr(top, left + 1, "─" * (bw - 2), bw - 2, attr)
    stdscr.addnstr(top + bh - 1, left + 1, "─" * (bw - 2), bw - 2, attr)
    stdscr.addnstr(top, left + 2, title, bw - 4, curses.color_pair(3) | curses.A_BOLD)
    # Columns (per the alignment reference): labels right-justified so
    # every ":" lands in one shared column, a fixed " : " separator, and
    # values left-justified in their column. The whole "label : value"
    # block is centered inside the box. The active row is marked by a
    # full-width highlight band behind its text (reverse spaces).
    label_w = max(len(label) for label, _, _ in fields)
    value_w = max(len(_display_field_value(mon, key)) for _, key, _ in fields)
    x0 = left + 2 + max(0, ((bw - 4) - (label_w + 3 + value_w)) // 2)
    vcol = x0 + label_w + 3            # value column (after " : ")
    vwidth = max(1, left + bw - 2 - vcol)    # to the interior right edge
    for fi, (label, key, active) in enumerate(fields):
        row0 = top + 2 + fi
        if row0 >= H - 1:
            break
        if fi == active_i:
            stdscr.addnstr(row0, left + 1, " " * (bw - 2), bw - 2, curses.A_REVERSE)
        la = curses.color_pair(6) | curses.A_BOLD
        if not active:
            la = curses.color_pair(6) | curses.A_DIM
        if fi == active_i:
            la |= curses.A_REVERSE
        stdscr.addnstr(row0, x0, label.rjust(label_w) + " :", vwidth + label_w, la)
        text = _display_field_value(mon, key)
        if len(text) > vwidth:
            text = text[:vwidth]
        va = curses.color_pair(4) | curses.A_BOLD
        if not active:
            va |= curses.A_DIM
        if fi == active_i:
            va |= curses.A_REVERSE
        stdscr.addnstr(row0, vcol, text, vwidth, va)
    if top + bh - 2 < H - 1:
        draw_hint(stdscr, top + bh - 2, left + 2, _WIZARD_HINT, left + bw - 1)
    if error and top + bh - 1 < H - 1:
        stdscr.addnstr(top + bh - 1, left + 1, error, bw - 2, curses.color_pair(2))


def display_wizard(stdscr, sh: int, sw: int, mons: list, sel: int) -> str:
    """Edit one monitor's options in the field-box wizard. Returns a log
    message. Save applies every monitor's line live (hyprctl keyword) and
    rewrites the generated config file; failures are reported in the log.
    mons[sel]['opts'] is edited in place."""
    mon = mons[sel]
    multi = len(mons) > 1
    # Fixed box height: the wizard always shows the full field set
    # (7 rows — hidden options dimmed), so the box never resizes.
    bh = 3 + 2 + len(_DISPLAY_ALL_FIELDS) + 1
    title = f"Display: {mon['name']}"
    i = 0

    def adjust(key, up: bool) -> bool:
        """Nudge the field one step; True when the value moved."""
        o = mon["opts"]
        if key == "hz":
            rates = hypr_monitor.rates_at_native(mon)   # highest first
            if len(rates) <= 1:
                return False
            j = min(range(len(rates)), key=lambda k: abs(rates[k] - o.get("hz", 0)))
            j = j - 1 if up else j + 1
            if 0 <= j < len(rates) and rates[j] != o.get("hz"):
                o["hz"] = rates[j]
                return True
            return False
        if key == "vrr":
            v = min(3, max(0, int(o.get("vrr", 0))))
            o["vrr"] = (v + 1) % 4 if up else (v - 1) % 4
            return True
        if key == "enabled":
            o[key] = bool(up)
            return True
        if key == "color":
            modes = hypr_monitor.COLOR_MODES
            cur = o.get("color", "srgb")
            if cur not in modes:
                cur = "srgb"
            o["color"] = modes[(modes.index(cur) + (1 if up else -1)) % len(modes)]
            return True
        if key == "bd":
            o["bd"] = 8 if int(o.get("bd", 10)) == 10 else 10
            return True
        # sdr_b / sdr_s: step 0.05, clamped
        v = o.get(key, 1.0) + (0.05 if up else -0.05)
        o[key] = round(min(3.0, max(0.1, v)), 2)
        return True

    stdscr.timeout(-1)
    try:
        while True:
            # Re-measure every pass: the screen may have been resized
            # while we're open (a stale size would push the border past
            # the edge and crash addch). Width: just wide enough for the
            # hint (the widest content) — border + 1 space padding each
            # side, +1 breathing room — and degrade on small screens.
            sh, sw = stdscr.getmaxyx()
            bw = min(len(_WIZARD_HINT) + 4, max(10, sw - 2))
            top = max(0, (sh - bh) // 2)
            left = max(1, (sw - bw) // 2)
            fields = _display_fields(mon, multi)
            if i >= len(fields):
                i = len(fields) - 1
            _display_draw(stdscr, sh, sw, top, left, bw, bh,
                          title, mon, fields, i, _display_error(mon))
            stdscr.refresh()
            c = _read_key(stdscr, restore_ms=-1)
            if c == curses.KEY_RESIZE:
                # Close on resize: the box is rebuilt from the current
                # size next time it opens; nothing is saved.
                return "[display] cancelled (window resized)"
            if c in (27, ord("q")):
                return "[display] cancelled"
            if c in (10, 13, curses.KEY_ENTER):
                break
            if c in (curses.KEY_UP, "up"):
                # Move up, wrapping, skipping dim (hidden) fields.
                n = len(fields)
                j = i
                for _ in range(n):
                    j = (j - 1) % n
                    if fields[j][2]:
                        break
                i = j
            elif c in (curses.KEY_DOWN, "down"):
                n = len(fields)
                j = i
                for _ in range(n):
                    j = (j + 1) % n
                    if fields[j][2]:
                        break
                i = j
            elif c in (curses.KEY_LEFT, "left"):
                # Adjust only active fields.
                if fields[i][2]:
                    adjust(fields[i][1], False)
            elif c in (curses.KEY_RIGHT, "right"):
                if fields[i][2]:
                    adjust(fields[i][1], True)
            # any other key: ignore, redraw next pass
    finally:
        stdscr.timeout(100)

    # save: apply every monitor's line live, then persist all lines and
    # the per-monitor state (live first so a config write never outlives
    # a failed apply)
    errs = []
    for m in mons:
        ok, out = hypr_monitor.apply_line(_display_line(m))
        if not ok:
            errs.append(f"{m['name']}: {out}")
    okp = hypr_monitor.persist_file(hypr_monitor.DISPLAY_CONF,
                                    [_display_line(m) for m in mons])
    saved = hypr_monitor.save_display_state({m["name"]: m["opts"] for m in mons})
    if errs:
        return "[err] " + "; ".join(errs)
    msg = f"[display] saved ({len(mons)} monitor{'' if len(mons) == 1 else 's'})"
    if not (okp and saved):
        msg += "  [warn] config not saved"
    return msg


# ============================================================
#  BANNER  —  "NVIDIA OVERCLOCK" with an eye/swoosh mark
# ============================================================
# Each row is (left, right): left = logo + "NVIDIA" (drawn in the
# header color), right = "OVERCLOCK" (drawn in the body color).
# Built from a 3-col block font so it stays aligned in monospace.
BANNER = [
    ("    ██      █ █ █ █ ███ ██  ███  █ ", " █  █ █ ███ ██   ██ █    █   ██ █ █"),
    ("  ██  ██    ███ █ █  █  █ █  █  █ █", "█ █ █ █ █   █ █ █   █   █ █ █   █ █"),
    ("██      ██  █ █ █ █  █  █ █  █  ███", "█ █ █ █ ██  ██  █   █   █ █ █   ██ "),
    ("  ██  ██    █ █  █   █  █ █  █  █ █", "█ █  █  █   █ █ █   █   █ █ █   █ █"),
    ("    ██      █ █  █  ███ ██  ███ █ █", " █   █  ███ █ █  ██ ███  █   ██ █ █"),
]

BANNER_ROWS = len(BANNER)   # 5

# Minimum window that can render every section at once — border, banner,
# status + OC profile, ReBAR, Display, hints, the ? help overlay (the
# tallest content), log, footer — in the worst case (2 GPUs, 2 monitors,
# read-only mode, help open). Below this, the TUI shows a "window too
# small" notice instead of a broken layout; the check re-runs every
# frame, so a live resize in either direction is picked up automatically.
MIN_ROWS = 36
MIN_COLS = 78


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    # ncurses waits up to this many ms to decide a lone 0x1B is a
    # bare Esc vs the start of an arrow/fn-key sequence; cap it so
    # Esc is not sluggish (the app's own 10ms peek adds on top).
    curses.set_escdelay(25)
    theme = apply_theme(stdscr, load_theme())

    # Non-root = read-only: display only (status, profiles, telemetry).
    # Applying OC and editing profiles need root (sudo).
    read_only = os.geteuid() != 0

    profiles, selected = load_state()
    if not profiles:
        # First run: no profiles.json yet — seed a default profile.
        profiles = {"default": {k: int(d) for k, _, d in PROFILE_FIELDS}}
        selected = "default"
        save_profiles(profiles, selected)
    elif selected not in profiles:
        selected = "default" if "default" in profiles else next(iter(profiles))
        save_profiles(profiles, selected)
    if profiles.get("default") == _BAD_SEED:
        # Stale first-run seed: restore the real default offsets.
        profiles["default"] = {k: int(d) for k, _, d in PROFILE_FIELDS}
        save_profiles(profiles, selected)

    help_overlay = False
    select_open = False
    log = ""
    # UI tick: getch returns -1 after 100ms -> smooth redraws.
    # Telemetry (nvidia-smi) is throttled separately below (TELEMETRY_MS),
    # so a faster UI never increases the pull rate.
    stdscr.timeout(100)
    TELEMETRY_MS = 2000   # how often nvidia-smi is actually called
    stats, stats_at = {}, 0.0
    prev_stats = {}   # last sample, for the bar-fill lerp
    fan_state = {}    # fan mode from fan_control.py, refreshed with stats
    procs = []        # top VRAM processes, refreshed with stats
    # ReBAR: BAR size is fixed at boot — query once, not per tick
    rebar = get_rebar()

    # Hyprland display section — always visible when hyprctl is
    # available; editing needs no root (hyprctl, user session)
    display_wizard_open = False
    display_state, display_sel = _display_init()
    display_at = 0.0

    # Flicker-free rendering: each frame is drawn into stdscr's virtual
    # buffer (erase + redraw); refresh() diffs it against the physical
    # screen and pushes only the changed cells (steady state: none).
    while True:
        active, active_name = get_status()
        # pull telemetry at its own cadence; between pulls we redraw
        # the last values from the cache (no extra nvidia-smi)
        now = time.monotonic()
        if now - stats_at >= TELEMETRY_MS / 1000:
            prev_stats = stats
            stats, stats_at = get_gpu_stats(), now
            fan_state = get_fan_state()
            new_procs = get_gpu_processes()
            # Log a process-count change (not every 2s tick): if the column
            # ever goes empty, the log shows whether nvidia-smi reported zero
            # (environment) vs. the last non-empty count.
            if len(new_procs) != len(procs):
                log = prepend_log(log, f"[telemetry] nvidia-smi: "
                                       f"{len(new_procs)} process(es)")
            procs = new_procs
        if display_state and now - display_at >= DISPLAY_MS / 1000:
            display_state, display_sel = _display_refresh(
                display_state, display_sel, now)
            display_at = now

        status_word = "ACTIVE" if active else "INACTIVE"
        if active and active_name:
            status_word += f" ({active_name})"
        if help_overlay:
            hint = "Press ? again to close help"
        elif select_open:
            hint = "[1-9] pick  [n] new  [d] delete  [Esc/q] close"
        else:
            hint = ("[1] Activate [2] Deactivate [x] Profiles [f] Fan "
                    "[t] Theme [q] Quit [?] Help")
            if read_only:
                hint = ("[q] Quit [?] Help  (read-only: sudo needed for "
                        "OC / profiles)")
            if display_state:
                # display editing works in read-only mode too (hyprctl)
                hint = "[e] Edit [↑↓] select  " + hint

        help_text = (
            [
                "  1 / a .... activate with selected profile",
                "  2 / d .... deactivate overclocking",
                "  n ...... define + save a new profile (menu open)",
                "  x ...... open the Profiles menu (Esc/q close)",
                "  1-9 .... pick a profile (while the menu is open)",
                "  d ...... delete the picked profile (menu open)",
                "  f ...... set fan speed (a %, or 'auto' to revert); sudo",
                "  e ...... edit the highlighted monitor (wizard, no sudo)",
                "  t ...... cycle color theme",
                "  q / Esc . quit",
                "  Esc+Enter cancels a prompt",
                "  ? ...... toggle this help",
            ]
            if help_overlay else []
        )
        log_lines = (log or "(no action yet)").splitlines()

        # --- render ------------------------------------------------------
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        sh, sw = max(1, h - 1), max(1, w - 1)   # safe bounds

        def border(win, H, W):
            """Single-line frame on the screen edge (drawn last: owns the
            corners and any content bleed). Needs 4x10 minimum."""
            # stdscr cannot write the true bottom row/col, so the frame
            # is drawn on the safe bounds (one cell in from the edge).
            if H < 4 or W < 10:
                return
            attr = curses.color_pair(4)
            win.addch(0, 0, "┌")
            win.addch(H - 2, 0, "└")
            win.addch(0, W - 2, "┐")
            win.addch(H - 2, W - 2, "┘")
            win.addnstr(0, 1, "─" * (W - 3), W - 3, attr)
            win.addnstr(H - 2, 1, "─" * (W - 3), W - 3, attr)
            for r in range(1, H - 2):
                win.addnstr(r, 0, "│", 1, attr)
                win.addnstr(r, W - 2, "│", 1, attr)

        # Too small for the layout: a notice instead of a broken UI.
        # Input is routed by what is open: an open wizard gets Esc as its
        # cancel (any other key just waits for a resize); otherwise
        # Esc/q quit the TUI. The size check re-runs every frame, so
        # resizing back to a usable size restores the full UI and the
        # wizard resumes where it left off.
        if h < MIN_ROWS or w < MIN_COLS:
            border(stdscr, h, w)
            if h >= 7:
                msg = (f"window too small — minimum required: "
                       f"{MIN_ROWS} rows x {MIN_COLS} cols")
                sub = ("resize the window to continue"
                       "   [Esc] close wizard  [q] Quit")
                r = h // 2 - 1
                stdscr.addnstr(r, max(0, (w - len(msg)) // 2), msg, w,
                               curses.color_pair(2) | curses.A_BOLD)
                stdscr.addnstr(r + 1, max(0, (w - len(sub)) // 2), sub, w,
                               curses.color_pair(4))
            stdscr.refresh()
            key = stdscr.getch()
            if key == 27 and display_wizard_open:
                # Esc cancels the open wizard without saving
                log = prepend_log(log, "[display] cancelled (window too small)")
                display_wizard_open = False
                display_state, display_sel = _display_refresh(
                    display_state, display_sel, time.monotonic())
            elif key in (ord("q"), 27):
                break
            continue

        PAD = 4     # horizontal indent (padding) for the banner block
        TOP = 1     # blank row above the banner
        def divider(row):
            if row < sh:
                stdscr.addnstr(row, 0, ("─" * sw), sw)

        # ASCII title: eye mark + "NVIDIA" (header) + "OVERCLOCK" (body)
        for r, (left, right) in enumerate(BANNER):
            stdscr.addnstr(TOP + r, PAD, left, sw, curses.color_pair(3) | curses.A_BOLD)
            # +2: breathing space between "NVIDIA" and "OVERCLOCK"
            stdscr.addnstr(TOP + r, PAD + len(left) + 2, right, sw, curses.color_pair(4))
        divider(TOP + BANNER_ROWS)

        # --- status + OC profile in use ---
        status_row = TOP + BANNER_ROWS + 1
        status_attr = curses.color_pair(1) if active else curses.color_pair(2)
        stdscr.addnstr(status_row, PAD, "Status:", sw,
                       curses.color_pair(4) | curses.A_BOLD)
        stdscr.addnstr(status_row, PAD + 9, status_word, sw,
                       status_attr | curses.A_BOLD)

        oc_row = status_row + 1
        sel_text = profile_summary(profiles[selected]) if selected else "(no profile)"
        if not active:
            sel_text += "   (inactive)"
        stdscr.addnstr(oc_row, PAD, "OC profile:", sw,
                       curses.color_pair(4) | curses.A_BOLD)
        stdscr.addnstr(oc_row, PAD + 12, sel_text, sw,
                       curses.color_pair(1) if active else curses.color_pair(2))

        # --- ReBAR status (one row per GPU, right under the OC profile) ---
        rebar_extra = 0
        for i, (rtext, ractive) in enumerate(rebar):
            row = oc_row + 1 + i
            if row < sh:
                stdscr.addnstr(row, PAD, "ReBAR:", sw,
                               curses.color_pair(4) | curses.A_BOLD)
                stdscr.addnstr(row, PAD + 8, rtext, sw,
                               curses.color_pair(1) if ractive else curses.color_pair(2))
                rebar_extra += 1

        # --- Hyprland display section (one row per monitor) ---
        disp_extra = 0
        if display_state:
            stdscr.addnstr(oc_row + 1 + rebar_extra, PAD, "Display:", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            disp_extra += 1
            for i, m in enumerate(display_state):
                row = oc_row + 1 + rebar_extra + disp_extra
                if row >= sh:
                    break
                cur = i == display_sel
                stdscr.addnstr(row, PAD + 2, _display_summary(m), sw,
                               curses.color_pair(1) if cur else curses.color_pair(4))
                disp_extra += 1

        # --- read-only notice (non-root): explain the mode at startup ---
        if read_only:
            stdscr.addnstr(oc_row + 1 + rebar_extra + disp_extra, PAD,
                           "non-root: read-only mode (use sudo to apply "
                           "OC / edit profiles)", sw,
                           curses.color_pair(2) | curses.A_BOLD)
        ro_extra = (1 if read_only else 0) + rebar_extra + disp_extra

        # --- button hints ---
        divider(oc_row + 1 + ro_extra)
        hint_row = oc_row + 2 + ro_extra
        draw_hint(stdscr, hint_row, PAD, hint, sw)

        # --- telemetry area: selection menu, help, or live stats ---
        divider(hint_row + 1)
        gpu_row = hint_row + 2
        telemetry = []
        if select_open:
            # profile selection menu — replaces the telemetry area
            names = list(profiles.items())[:9]
            stdscr.addnstr(gpu_row, PAD, "Profiles:  [1-9] pick  [n] new  [d] delete"
                                          "  [Esc/q] close", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            for i, (pname, pvals) in enumerate(names):
                mark = "*" if pname == selected else " "
                cur = pname == selected
                stdscr.addnstr(gpu_row + 1 + i, PAD + 1, f"{i+1}. {mark} {pname}", sw,
                               curses.color_pair(1) if cur else curses.color_pair(4))
                stdscr.addnstr(gpu_row + 1 + i, PAD + 16, profile_summary(pvals), sw,
                               curses.color_pair(4))
            last_used = gpu_row + len(names)
        elif not help_overlay:
            if stats:
                stdscr.addnstr(gpu_row, PAD, "GPU:", sw,
                               curses.color_pair(4) | curses.A_BOLD)
                # Second column: top-6 VRAM processes (separate nvidia-smi
                # query, same cadence). Starts clear of the value column,
                # which can run to ~13 chars ("210 W / 304 W") from PAD+24.
                PROC_COL = PAD + 42
                stdscr.addnstr(gpu_row, PROC_COL, "Top VRAM procs:", sw,
                               curses.color_pair(4) | curses.A_BOLD)
                # Bar fills glide between samples: lerp from the
                # previous pull (one window ago) to the current one;
                # the numbers stay on the latest sample.
                frac = 1.0
                if prev_stats:
                    frac = max(0.0, min(1.0, (now - stats_at) / (TELEMETRY_MS / 1000)))

                def val(key: str) -> float:
                    return _lerp_field(prev_stats, stats, key, frac)

                pmax = _f(stats.get("power_max"))
                limit = "" if pmax <= 0 else " / " + _trim0(stats["power_max"]) + " W"
                # Fan mode tag from fan_control.py: ' (auto)' / ' (manual)'.
                # Empty when the script/pynvml is unavailable (plain %).
                fmode = fan_state.get("policy", "")
                fan_tag = f" ({fmode})" if fmode else ""
                telemetry = [
                    ("◷ Clock",  _bar(val("clock"), _f(stats.get("clock_max"))),
                     f"{stats['clock']} MHz"),
                    ("✵ Fan",    _bar(val("fan"), 100),
                     f"{stats['fan']} %{fan_tag}"),
                    ("◉ Temp",   _bar(val("temp"), 90), f"{stats['temp']} C"),
                    ("⌁ Power",  _bar(val("power"), pmax),
                     f"{stats['power']} W{limit}"),
                    ("◔ Util",   _bar(val("util"), 100), f"{stats['util']} %"),
                    ("▤ Memory", _bar(val("mem"), _f(stats.get("mem_total"))),
                     f"{stats['mem']} MiB"),
                ]
                for i, (label, bar, valstr) in enumerate(telemetry):
                    # label(9) + bar(12) in the body color, value in ok
                    stdscr.addnstr(gpu_row + 1 + i, PAD + 1,
                                   f"{label:<9} {bar:<12}", sw,
                                   curses.color_pair(4))
                    stdscr.addnstr(gpu_row + 1 + i, PAD + 24, valstr,
                                   sw, curses.color_pair(1))
                    # process column: name (left, 15 wide) + MiB (right)
                    if i < len(procs):
                        pname, pmem = procs[i]
                        stdscr.addnstr(gpu_row + 1 + i, PROC_COL,
                                       pname[:15].ljust(15), sw,
                                       curses.color_pair(4))
                        stdscr.addnstr(gpu_row + 1 + i, PROC_COL + 17,
                                       f"{pmem:>5} MiB", sw,
                                       curses.color_pair(1))
                # pane is 7 rows: header + 6 telemetry + 1 empty spacer row
                last_used = gpu_row + len(telemetry) + 1
            else:
                stdscr.addnstr(gpu_row, PAD, "GPU: (nvidia-smi unavailable)", sw,
                               curses.color_pair(2))
                last_used = gpu_row
        else:
            last_used = gpu_row

        # --- help overlay ---
        if help_overlay:
            for i, line in enumerate(help_text):
                stdscr.addnstr(gpu_row + i, PAD, line, sw, curses.color_pair(4))
            last_used = gpu_row + len(help_text) - 1

        # --- log (clamped: never overflows below the footer) ---
        divider(last_used + 1)
        log_top = last_used + 2
        if log_top < sh - 1:
            stdscr.addnstr(log_top, PAD, "Log:", sw,
                           curses.color_pair(4) | curses.A_BOLD)
            fit = (sh - 2) - (log_top + 1)
            for i, line in enumerate(log_lines[:max(0, fit)]):
                stdscr.addnstr(log_top + 1 + i, PAD, line, sw, curses.color_pair(4))

        stdscr.addnstr(sh - 1, 0, ("─" * sw), sw)   # footer (never last row)
        border(stdscr, h, w)
        # version pinned bottom-left; status note bottom-right. The
        # minimum-width guard keeps them from colliding on a narrow frame.
        left = f" Nvidia TUI Overclocker v{__version__}"
        stdscr.addnstr(sh - 1, 1, left, sw - 2,
                       curses.color_pair(4) | curses.A_BOLD)
        note = f" theme: {theme} [t]   profile: {selected} [x] "
        if select_open:
            note += " [menu open]"
        stdscr.addnstr(sh - 1, max(0, w - len(note)), note, len(note),
                       curses.color_pair(4) | curses.A_BOLD)

        # frame complete: refresh diffs the virtual buffer against the
        # physical screen and rewrites only the cells that changed
        # (steady state: none -> no flicker, no churn).
        stdscr.refresh()

        # --- input -------------------------------------------------------
        if display_wizard_open:
            # before getch: no key is swallowed between 'e' and the wizard
            log = prepend_log(log, display_wizard(stdscr, sh, sw,
                                                  display_state, display_sel))
            display_wizard_open = False
            display_state, display_sel = _display_refresh(
                display_state, display_sel, time.monotonic())
            continue
        # arrows are raw ESC sequences (no keypad): decode them only when
        # the Display section is visible, so a bare Esc still quits
        c = (_read_key(stdscr) if (display_state and not select_open)
             else stdscr.getch())
        if c == -1:        # timeout — just redraw
            continue
        if c in (ord("q"), 27):
            if select_open:
                select_open = False
            else:
                break
            continue
        if c == ord("e") and display_state and not select_open:
            # not root-gated: hyprctl talks to the user's compositor
            display_wizard_open = True
            continue
        if c == "up" and display_state and not select_open:
            display_sel = (display_sel - 1) % len(display_state)
            continue
        if c == "down" and display_state and not select_open:
            display_sel = (display_sel + 1) % len(display_state)
            continue
        if read_only and c in (ord("1"), ord("a"), ord("2"), ord("d"),
                               ord("n"), ord("x"), ord("t"), ord("f")):
            # read-only: hint, no error, no write
            log = prepend_log(log, f"[readonly] '{chr(c)}' needs sudo")
            continue
        if select_open and ord("1") <= c <= ord("9"):
            # menu is open: digits pick a profile (menu stays open)
            names = list(profiles)
            if c - ord("1") < len(names):
                selected = names[c - ord("1")]
                save_profiles(profiles, selected)
                log = prepend_log(log, f"[profile] selected '{selected}'")
            else:
                log = prepend_log(log, "[err] no such profile number")
            continue
        if select_open and c == ord("n"):
            # menu open: new profile wizard lives here now
            selected, entry = new_profile(stdscr, sh, sw, profiles)
            if selected:
                if not save_profiles(profiles, selected):
                    log = prepend_log(log, f"[err] could not write '{selected}' "
                                           "to " + PROFILES_FILE + " (profile not saved)")
            log = prepend_log(log, entry)
            continue
        if select_open and c == ord("d"):
            # menu is open: delete the picked profile
            if len(profiles) <= 1:
                log = prepend_log(log, "[err] cannot delete the last profile")
            else:
                deleted = selected
                del profiles[deleted]
                remaining = list(profiles)
                selected = remaining[0]
                save_profiles(profiles, selected)
                log = prepend_log(log, f"[profile] deleted '{deleted}', "
                                       f"selected '{selected}'")
            continue
        if c in (ord("1"), ord("a")):
            if selected:
                rc, out = activate_overclock()
                log = prepend_log(log, f"[ok] activate '{selected}' rc={rc}" if rc == 0
                                  else f"[err] activate '{selected}' rc={rc}\n{out}")
            else:
                log = prepend_log(log, "[err] no profile — press n to create one")
        elif c in (ord("2"), ord("d")):
            rc, out = deactivate_overclock()
            log = prepend_log(log, f"[ok] deactivate rc={rc}" if rc == 0
                              else f"[err] deactivate rc={rc}\n{out}")
        elif c == ord("n"):
            continue   # 'n' lives in the Profiles menu now — no-op here
        elif c == ord("x"):
            select_open = True   # x opens; menu closes only via q/Esc
        elif c in (ord("?"), ord("h")):
            help_overlay = not help_overlay
        elif c == ord("t"):
            theme = apply_theme(stdscr, next_theme(theme))
            save_theme(theme)
            log = prepend_log(log, f"[theme] switched to {theme}")
        elif c == ord("f"):
            # Fan speed: a % sets manual, 'auto' reverts to the driver curve,
            # empty/Esc cancels. Needs root (set/auto fail cleanly otherwise).
            res = prompt(stdscr, sh, sw,
                         "Fan % (30-100, or 'auto' to revert)")
            if res is None or res == "":
                continue   # Esc or empty: cancel, no change
            res = res.strip().lower()
            if res == "auto":
                rc, out = set_fan_auto()
                log = prepend_log(log, "[ok] fan auto" if rc == 0
                                  else f"[err] fan: {out}")
            else:
                try:
                    pct = int(res)
                except ValueError:
                    log = prepend_log(log, f"[fan] not a number: {res}")
                    continue
                rc, out = set_fan_manual(pct)
                log = prepend_log(log, f"[ok] fan manual {pct}%" if rc == 0
                                  else f"[err] fan: {out}")

# ============================================================


if __name__ == "__main__":
    if "--version" in sys.argv[1:]:
        print(f"Nvidia TUI Overclocker v{__version__}")
        sys.exit(0)
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    sys.exit(0)
