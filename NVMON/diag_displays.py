"""nvmon display-capture diagnostic — run ON the affected render node.

    cd NVMON
    python\python.exe diag_displays.py [profiles\<yourprofile>.json]

Prints, at each boundary, where display indices come from — so we can see
exactly where a -1,-1 originates vs. the real (0,0)/(0,1) topology.
"""
import json
import sys

from nvmon import config, executors, parsers
from nvmon.agent import qsync as qsync_mod
from nvmon.agent import displays as displays_mod
from nvmon.agent import mosaic as mosaic_mod


def show(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# 1. Raw configureQsync2 queryAll — the ground-truth source for qsync displays
show("1. RAW  configureQsync2.exe queryAll")
exe = config.get_qsync_exe()
print("qsync exe:", exe or "<NOT FOUND>")
raw = ""
if exe:
    try:
        raw = executors.run(exe, ["queryAll"], label="diag", quiet=True).stdout or ""
    except Exception as e:
        print("run failed:", e)
print(raw or "<empty>")

# 2. What the parser extracts from that raw output
show("2. parse_qsync_displays(raw)  -> should be [(0,0),(0,1)] for you")
try:
    print(parsers.parse_qsync_displays(raw))
except Exception as e:
    print("parse failed:", e)

# 3. What the NEW capture path will record
show("3. qsync._discover_displays()  <- what save-profile now writes")
try:
    print(qsync_mod._discover_displays())
except Exception as e:
    print("discover failed:", e)

# 4. Per-GPU sync list (old capture path — GPU index only, display forced 0)
show("4. qsync.status().per_gpu_sync  (gpu, signal_available, sync_state)")
try:
    st = qsync_mod.status()
    print("available:", st.available, "source:", st.source, "role:", st.role)
    for g in st.per_gpu_sync or []:
        print(f"  gpu={g.gpu}  signal={g.signal_available}  state={g.sync_state!r}")
except Exception as e:
    print("status failed:", e)

# 5. DirectDisplay table — where gpu_index/display_index default to -1 if unparsed
show("5. displays.status().displays  (gpu_index, display_index, id)  <- -1 = unresolved")
try:
    dd = displays_mod.status()
    print("available:", getattr(dd, "available", "?"))
    for d in (dd.displays or []):
        print(f"  gpu_index={d.gpu_index}  display_index={d.display_index}  id={d.id}")
except Exception as e:
    print("displays.status failed:", e)

# 6. Mosaic — parsed state
show("6. mosaic.status()  (parsed)")
try:
    mos = mosaic_mod.status()
    print("enabled:", getattr(mos, "enabled", "?"),
          " rows:", getattr(mos, "rows", "?"), " cols:", getattr(mos, "cols", "?"),
          " res:", getattr(mos, "per_display_resolution", "?"))
    for o in (getattr(mos, "outputs", None) or []):
        print(f"  gpu={o.gpu}  display={o.display}  display_id={getattr(o,'display_id','')}")
except Exception as e:
    print("mosaic.status failed:", e)

# 6b. RAW configureMosaic output — THE key artifact for the parser
show("6b. RAW  configureMosaic.exe query")
mexe = config.get_mosaic_exe()
print("mosaic exe:", mexe or "<NOT FOUND>")
if mexe:
    try:
        r = executors.run(mexe, ["query"], label="diag", quiet=True)
        print("--- stdout ---\n" + (r.stdout or "<empty>"))
        print("--- stderr ---\n" + (r.stderr or "<empty>"))
    except Exception as e:
        print("query failed:", e)
    show("6c. RAW  configureMosaic.exe query current")
    try:
        r = executors.run(mexe, ["query", "current"], label="diag", quiet=True)
        print("--- stdout ---\n" + (r.stdout or "<empty>"))
        print("--- stderr ---\n" + (r.stderr or "<empty>"))
    except Exception as e:
        print("query current failed:", e)

# 7. The saved profile, if you pass one
if len(sys.argv) > 1:
    show(f"7. SAVED PROFILE  {sys.argv[1]}")
    try:
        d = json.load(open(sys.argv[1]))
        print("qsync.displays :", d.get("qsync", {}).get("displays"))
        print("mosaic.enabled :", d.get("mosaic", {}).get("enabled"))
        print("mosaic.outputs :", d.get("mosaic", {}).get("outputs"))
    except Exception as e:
        print("load failed:", e)
