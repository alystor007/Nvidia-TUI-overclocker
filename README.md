# Nvidia TUI Overclocker

A lazydocker-style terminal UI for NVIDIA GPU overclocking, with live GPU
telemetry (bar meters), savable OC profiles, fan control, Hyprland display
control, and a themeable interface.

![Nvidia TUI Overclocker](nvidia-tui-overclocker.png)

## Files

| File | Purpose |
|------|---------|
| `gpu_tui.py` | The TUI (run this). Live stats, profile menu, Display section, themes, action log. |
| `apply_overclock.py` | Applies the selected OC profile (also usable standalone). |
| `reset_overclock.py` | Restores factory defaults: power limit, locked clocks, clock offsets. |
| `fan_control.py` | Reads/sets the GPU fan speed (drives the TUI's `f` key). |
| `rebar_check.py` | Checks whether Resizable BAR (ReBAR) is active; also shown in the TUI. |
| `hypr_monitor.py` | Hyprland display state — drives the Display section (stdlib only, no pynvml). |

## Requirements

- Linux with an NVIDIA GPU and a recent driver (NVML)
- Python 3.10+
- `pynvml` — the only third-party dependency for the GPU/OC side:
  ```
  pip install -r requirements.txt
  ```
- The **Display** section needs Hyprland running (`hyprctl` on PATH). It is
  optional — without it the section stays hidden and everything else works.

## Usage

Run the TUI (applying OC and editing profiles need root):

```
sudo python3 gpu_tui.py
```

Without sudo the TUI runs in read-only mode: live stats, status, and profile
viewing work, but OC/profile actions are disabled.

Show the version:

```
python3 gpu_tui.py --version
```

Standalone:

```
sudo python3 apply_overclock.py              # apply the selected profile
sudo python3 apply_overclock.py <profile>    # apply a profile and select it
python3 apply_overclock.py --list            # list profiles ('*' = selected)
sudo python3 reset_overclock.py              # remove OC, restore factory defaults
python3 rebar_check.py                       # ReBAR status (0 active, 1 not, 2 no GPU)
```

## Keys

| Key | Action |
|-----|--------|
| `1` / `a` | Activate overclock (selected profile) |
| `2` / `d` | Deactivate overclock |
| `x` | Open the Profiles menu |
| `1-9` | Pick a profile (menu open) |
| `n` | Define + save a new profile (menu open) |
| `d` | Delete the picked profile (menu open) |
| `t` | Cycle color theme |
| `f` | Set fan speed (manual %, or `auto` to revert) — needs sudo |
| `e` | Edit the highlighted monitor (wizard) |
| `↑↓` | Move the monitor selection (Display section) |
| `←→` | Adjust the active field (wizard open) |
| `Enter` | Save wizard changes (wizard open) |
| `?` / `h` | Help overlay |
| `q` / `Esc` | Close menu / quit |

## Profiles

Profiles live in a single file, `~/.config/gpu-tui/profiles.json` — the same
file the OC scripts read:

```json
{
  "selected": "default",
  "default": {
    "power_w": 260,    "clock_min": 210,  "clock_max": 2730,
    "mem_off": 400,    "gfx_off": 200
  }
}
```

A theme file (`~/.config/gpu-tui/theme`) stores the last-used color theme.
While an OC is applied, the active profile name is written to
`/tmp/gpu_oc_active` (removed on reset) so other tools can detect the state.

## GPU telemetry

The GPU pane shows live stats as bar meters — clock, fan, temperature,
power (current / limit), utilization, and VRAM — refreshed every 2 seconds.
A second column lists the **top 6 VRAM-consuming processes** (name and MiB),
sourced from the same driver query as the stats. With no processes running
the column stays blank; when the driver can't map a process name it shows
the PID instead.

## ReBAR status

Under the OC profile line the TUI shows the Resizable BAR (ReBAR) status
per GPU, e.g. `ACTIVE 0000:05:00.0 RTX 4070 Ti SUPER BAR 16 GiB,
VRAM 16376 MiB`. With ReBAR enabled the CPU maps the whole framebuffer
instead of a small legacy BAR (256 MiB), which full GPU-Direct workloads
need. It is a BIOS/UEFI feature and cannot be changed from the OS. The
check reads BAR sizes from `/sys/bus/pci`, so it works without root or
even a GPU driver; the status is queried once at startup, since the BAR
size is fixed at boot.

## Display (Hyprland)

When Hyprland is running, the TUI shows a **Display** section under the ReBAR
line with one row per connected monitor, e.g.:

```
DP-1  2560x1440@165  HDR 10-bit sdr 1.00/1.00  VRR on
HDMI-A-1  1920x1080@60  sRGB  VRR off
```

The section talks to your compositor session directly via `hyprctl`, so it
**needs no sudo** — it stays editable even when the rest of the TUI is in
read-only mode.

Highlight a monitor and press `e` to open the edit wizard: move fields with
`↑↓`, adjust the active field with `←→`, and save with `Enter`. Fields per
monitor:

- **Refresh** — one of the rates at the monitor's native resolution
- **VRR** — `off` / `on` / `fullscreen only` / `fullscreen with video or game`
- **Color** — `srgb` / `wide` / `hdr`
- **Bit depth** — e.g. `10-bit` (HDR/wide only)
- **SDR brightness** / **SDR saturation** (HDR only)
- **Enabled** — disable a monitor (shown only with 2+ monitors)

Changes apply live through `hyprctl` and are also written to a generated
config file, `~/.config/hypr/monitors-gpu-tui.conf`. You wire it in once by
adding `source = ~/.config/hypr/monitors-gpu-tui.conf` to your
`monitors.conf` — the tool never edits that file itself.

## Fan control

Press `f` and enter a percentage (e.g. `60`) to pin all fans to that speed
in manual mode, or `auto` to restore the driver's temperature curve. The
Fan row tags the current mode inline — `45 % (manual)` / `45 % (auto)` —
and falls back to a plain percentage when the script can't read the mode.
Setting a speed needs root (the TUI runs `fan_control.py` under sudo);
reading fan status does not.

## How the overclock works

A profile is three cooperating controls, all applied via NVML:

- **Power limit** (`power_w`) — the card's power ceiling. The GPU can never
  draw more than this, so the power management algorithm backs the clock
  off whenever the limit is hit. This is the main lever: a well-chosen
  limit gives most of the performance for far less heat than chasing
  clocks. The TUI shows the active limit inline on the Power row —
  `247 W / 260 W` (current draw / limit) — instead of a separate
  max-power row.

- **Clock range** (`clock_min` .. `clock_max`) — the GPU's graphics clock
  is locked inside this window. It cannot drop below `clock_min` (no
  downclocking under load spikes) or rise above `clock_max` (the boost
  ceiling).

- **Clock offset** (`gfx_off` / `mem_off`) — shifts the operating point
  along the chip's clock-vs-voltage curve: while running at clock *x*,
  the driver requests the voltage the curve assigns to clock *x + offset*.
  The chip therefore behaves like a slightly faster (or slower) chip at
  the same clock — extra stability headroom, or lower voltage for the
  same performance. The memory offset works the same way on the memory
  clock (the driver stores it x2 internally; the profile keeps plain MHz).

`reset_overclock.py` removes all three: power limit back to the factory
default, clock lock released, offsets to 0.

## Notes

- The scripts apply to GPU index 0. For multi-GPU setups, edit the
  `nvmlDeviceGetHandleByIndex` calls.
- `reset_overclock.py` restores a 285 W power limit as the factory default;
  adjust `FACTORY_POWER_W` if your card's default differs.
