#!/usr/bin/env python3
"""
gpu-tui v3 — a lazydocker-style TUI for GPU overclocking.

Keys:
  1 or a  -> Activate overclock (selected profile)
  2 or d  -> Deactivate overclock
  n       -> Define and save a new OC profile
  x       -> Select which profile activation uses
  t       -> Cycle theme (saved between runs)
  q       -> Quit
  ?       -> Help overlay

Profiles live in ~/.config/gpu-tui/profiles.json — the single file the OC
scripts read. /tmp/gpu_oc_active marks that an OC is currently applied.
"""

import curses
import json
import os
import re
import subprocess
import sys
import time

# ============================================================
#  SCRIPTS
# ============================================================

# Scripts live next to this TUI — move them together, no config needed.
_HERE = os.path.dirname(os.path.abspath(__file__))
ACTIVATE_SCRIPT = os.path.join(_HERE, "apply_overclock.py")
DEACTIVATE_SCRIPT = os.path.join(_HERE, "reset_overclock.py")
STATUS_MARKER = "/tmp/gpu_oc_active"   # apply_overclock.py writes the active profile name here

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
    fields = ["clocks.sm", "fan.speed", "temperature.gpu",
              "power.draw", "power.limit", "utilization.gpu", "memory.used"]
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
            "fan":       vals[1],   # %
            "temp":      vals[2],   # C
            "power":     vals[3],   # W (current draw)
            "power_max": vals[4],   # W (max/limit)
            "util":      vals[5],   # %
            "mem":       vals[6],   # MiB
        }
    except Exception:
        return {}


def prepend_log(log: str, entry: str) -> str:
    """Prepend an entry, keeping one line per entry."""
    return entry if not log else entry + "\n" + log


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


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
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

    # Flicker-free rendering: each frame is drawn into stdscr's virtual
    # buffer (erase + redraw); refresh() diffs it against the physical
    # screen and pushes only the changed cells (steady state: none).
    while True:
        active, active_name = get_status()
        # pull telemetry at its own cadence; between pulls we redraw
        # the last values from the cache (no extra nvidia-smi)
        now = time.monotonic()
        if now - stats_at >= TELEMETRY_MS / 1000:
            stats, stats_at = get_gpu_stats(), now

        status_word = "ACTIVE" if active else "INACTIVE"
        if active and active_name:
            status_word += f" ({active_name})"
        if help_overlay:
            hint = "Press ? again to close help"
        elif select_open:
            hint = "[1-9] pick  [n] new  [d] delete  [Esc/q] close"
        else:
            if read_only:
                hint = ("[q] Quit [?] Help  (read-only: sudo needed for "
                        "OC / profiles)")
            else:
                hint = ("[1] Activate [2] Deactivate [x] Profiles "
                        "[t] Theme [q] Quit [?] Help")

        help_text = (
            [
                "  1 / a .... activate with selected profile",
                "  2 / d .... deactivate overclocking",
                "  n ...... define + save a new profile (menu open)",
                "  x ...... open the Profiles menu (Esc/q close)",
                "  1-9 .... pick a profile (while the menu is open)",
                "  d ...... delete the picked profile (menu open)",
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

        # --- read-only notice (non-root): explain the mode at startup ---
        if read_only:
            stdscr.addnstr(oc_row + 1, PAD,
                           "running as non-root user — read-only mode "
                           "(use sudo to apply OC / edit profiles)", sw,
                           curses.color_pair(2) | curses.A_BOLD)
        ro_extra = 1 if read_only else 0

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
                telemetry = [
                    ("◷ Clock",  f"{stats['clock']} MHz"),
                    ("✵ Fan",    f"{stats['fan']} %"),
                    ("◉ Temp",   f"{stats['temp']} C"),
                    ("⌁ Power",  f"{stats['power']} W"),
                    ("⏻ Max W",  f"{stats['power_max']} W"),
                    ("◔ Util",   f"{stats['util']} %"),
                    ("▤ Memory", f"{stats['mem']} MiB"),
                ]
                for i, (label, val) in enumerate(telemetry):
                    stdscr.addnstr(gpu_row + 1 + i, PAD + 1, label,
                                   sw, curses.color_pair(4))
                    stdscr.addnstr(gpu_row + 1 + i, PAD + 10, val,
                                   sw, curses.color_pair(1))
            else:
                stdscr.addnstr(gpu_row, PAD, "GPU: (nvidia-smi unavailable)", sw,
                               curses.color_pair(2))
            last_used = gpu_row + len(telemetry)
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
        c = stdscr.getch()
        if c == -1:        # timeout — just redraw
            continue
        if c in (ord("q"), 27):
            if select_open:
                select_open = False
            else:
                break
            continue
        if read_only and c in (ord("1"), ord("a"), ord("2"), ord("d"),
                               ord("n"), ord("x"), ord("t")):
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

# ============================================================


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    sys.exit(0)
