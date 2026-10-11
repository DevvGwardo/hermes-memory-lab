"""Only disposable child processes, used by cancellation behavior tests."""
import json
from pathlib import Path
import subprocess
import sys


def nested_wait_worker(folder):
    from memory_sim.runner import atomic_json
    folder = Path(folder)
    folder.mkdir()
    script = (
        "import os,signal,sys,time;from pathlib import Path;"
        "p=Path(sys.argv[1]);"
        "signal.signal(signal.SIGTERM,lambda s,f:(p.write_text('stopped'),sys.exit(0)));"
        "Path(sys.argv[2]).write_text(str(os.getpid()));time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(folder / "child-stopped"), str(folder / "child-pid")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atomic_json(folder / "result.json", {"status": "running", "grandchild_pid": child.pid, "cycles": []})
    child.wait()
