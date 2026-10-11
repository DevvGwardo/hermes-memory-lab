import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import simulator

ROOT = Path(__file__).resolve().parents[1]


def running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "setsid"), "Owned process groups need POSIX")
class CancellationTests(unittest.TestCase):
    def test_interrupt_and_termination_stop_descendants(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as td:
                folder = Path(td) / "owned"
                script = (
                    "import sys;from pathlib import Path;"
                    f"sys.path[:0]=[{str(ROOT)!r},{str(ROOT / 'tests')!r}];"
                    "from memory_sim.processes import owned_job;"
                    "from process_fixtures import nested_wait_worker;"
                    "p=Path(sys.argv[1]);"
                    "r=owned_job(nested_wait_worker,(str(p),),p/'result.json',30);"
                    "sys.exit(2 if r['status']=='interrupted' else 0)"
                )
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(folder)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while not (folder / "child-pid").exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue((folder / "child-pid").exists())
                    child_pid = int((folder / "child-pid").read_text())
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 2, stdout + stderr)
                    result = json.loads((folder / "result.json").read_text())
                    self.assertEqual(result["status"], "interrupted")
                    self.assertTrue((folder / "child-stopped").exists())
                    deadline = time.monotonic() + 5
                    while running(child_pid) and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertFalse(running(child_pid))
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=15)

    @unittest.skipUnless(
        Path(simulator.DEFAULT_CORE).is_dir() and Path(simulator.DEFAULT_PLUGIN).is_file(),
        "CLI cancellation replay needs installed targets")
    def test_cli_cancellation_preserves_report_and_stops_future_seeds(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "cancelled"
            process = subprocess.Popen([
                sys.executable, "-B", str(ROOT / "simulator.py"), "run",
                "--seeds", "1,2,3", "--facts", "50", "--turns", "5000",
                "--cycles", "1", "--timeout", "60", "--output", str(output),
            ], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            try:
                checkpoint = output / "seed-1/checkpoints/cycle-1.json"
                deadline = time.monotonic() + 20
                while not checkpoint.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(checkpoint.exists())
                child_pid = json.loads(checkpoint.read_text())["worker_pid"]
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 2, stdout + stderr)
                result = json.loads((output / "run.json").read_text())
                self.assertTrue(result["interrupted"])
                self.assertEqual(result["requested_seeds"], [1, 2, 3])
                self.assertEqual(len(result["seeds"]), 1)
                self.assertFalse((output / "seed-2").exists())
                self.assertTrue((output / "report.html").exists())
                deadline = time.monotonic() + 5
                while running(child_pid) and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(running(child_pid))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=20)
