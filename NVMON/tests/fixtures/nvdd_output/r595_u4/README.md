# R595 U4 (596.36) capture set

NVIDIA RTX / Quadro Enterprise Production Branch **R595 U4** driver
**v596.36** (WHQL, released 2026-04-28). This is the first nvmon-supported
driver branch beyond R580 — capture real CLI output from a Blackwell node
running this branch, drop it here, and parser tests pin against it.

## What to capture

From a node running `python\python.exe -m nvmon agent` on the new driver,
collect verbatim stdout (no redaction beyond serial / UUID if the file is
shared externally):

| Filename | Source command |
|---|---|
| `nvidia_smi.txt` | `nvidia-smi --query-gpu=index,name,pci.bus_id,driver_version,temperature.gpu,power.draw,power.limit,utilization.gpu,memory.used,memory.total,clocks.current.graphics,clocks.current.memory,clocks.current.video,pstate,display_active,fan.speed,pcie.link.gen.current,pcie.link.width.current --format=csv,noheader,nounits` |
| `dd_status.txt` | `bin\configureDdisplay.exe -listMonitor` |
| `mosaic_query.txt` | `bin\configureMosaic.exe -q` |
| `mosaic_query_current.txt` | `bin\configureMosaic.exe -qd` |
| `qsync_query_all.txt` | `bin\configureQsync2.exe queryAll` (run elevated) |
| `qsync_query_all_house_sync_source.txt` | same as above with HOUSESYNC active |
| `qsync_query_params.txt` | `bin\configureQsync2.exe queryParams` |
| `nvcp_topology.json` | NVCP → System Topology → Save Logfile |

## Why a per-branch directory

The R580 fixtures live at `tests/fixtures/nvdd_output/*.txt` (flat). They
remain the legacy baseline. New branches go in their own subdirectory so
parser tests can pin against multiple driver outputs once we have them,
catching regressions that only surface on one branch.

## Things to specifically validate against R595 U4 / Blackwell

1. **Sync firmware bug** — Before capturing `qsync_query_all.txt`, confirm
   the RTX PRO Sync card firmware in NVCP. If it's **v3.04 or v3.06**, the
   "Timing Server: External" / "On this system" setting silently fails to
   apply on Blackwell. Test toggling it once before fan-out — see
   https://forums.developer.nvidia.com/t/362499. nvmon now emits a
   driver-advisory warning in `status.json.errors[]` for this combo (see
   `nvmon/agent/driver_info.py::KNOWN_ISSUES`).
2. **Multi-resolution Frame Lock** — R595 U4 added support for
   synchronized displays at *different* resolutions in one lock group.
   `framelock.py` reads `FrameLockDisplayConfig` per-display, so this
   should already work — verify by capturing a profile on a mixed-res
   wall and round-tripping `apply` / `verify` via the agent.
3. **EDID** — registry path
   `HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\<m>\<i>\Device Parameters\EDID_OVERRIDE`
   is unchanged in R595. `edid.py::apply_edid_to_all_displays` should be a
   no-op upgrade. Confirm by running `/edid` against the agent before and
   after the driver bump.
4. **Mosaic** — R595 U4 fixed "59.94 Hz refresh rate was unavailable in
   some Mosaic configurations." If the production profiles use 59.94 Hz,
   re-capture `mosaic_query.txt` to make sure the fix didn't change the
   output column ordering.
5. **Multi-Plane Overlay (MPO) on Blackwell** — Driver-side display
   composition change. nvmon doesn't read MPO state, so no code impact,
   but flag it here so we don't blame nvmon if the wall starts compositing
   differently.
