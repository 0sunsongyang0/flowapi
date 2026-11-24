"""Signal lifecycle tests using fresh subprocesses and no network requests."""

import asyncio
import contextlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).with_name("server.py")


def fixture_child(root, mode):
    spec = importlib.util.spec_from_file_location("flow2api_signal_fixture", MODULE_PATH)
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
    original_handler = signal.getsignal(signal.SIGTERM)
    server.settings = lambda: {"output_directory": str(root)}

    def forbid_network(*_args, **_kwargs):
        raise AssertionError("Signal fixtures must never make HTTP requests")

    server.api_client = forbid_network

    async def models(_cfg):
        return [{"id": server.DEFAULT_MODEL}]

    async def accounts(_cfg, _model):
        return {"accounts": [{"id": 1, "concurrency_limit": 1}]}

    async def execute(job, _cfg, account_id):
        result = {"job_index": job.index, "model": job.model, "account_id": account_id,
                  "output_path": str(job.path), "submission_attempted": True}
        if (job.index == 0 and mode != "single-blocked") or mode == "success":
            job.path.write_bytes(b"offline synthetic output")
            return {**result, "status": "succeeded", "path": str(job.path)}, None
        if job.index == 1:
            return {**result, "status": "failed", "error": "offline explicit failure"}, None
        (root / "inflight-started").write_text("ready")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            (root / "cleanup-started").write_text("ready")
            # This asynchronous cleanup makes a second SIGTERM observable and deterministic.
            await asyncio.sleep(0.2)
            (root / "cleanup-finished").write_text("finished")
            raise

    server.models, server.image_accounts, server.execute_job = models, accounts, execute
    if mode == "stdio":
        @contextlib.asynccontextmanager
        async def stdio():
            yield object(), object()

        async def run(*_args):
            assert signal.getsignal(signal.SIGTERM) == original_handler

        async def forbidden_generation(*_args):
            raise AssertionError("stdio must not install generation signal handling")

        server.stdio_server = stdio
        server.app = SimpleNamespace(run=run, create_initialization_options=lambda: None)
        server.run_generation_cli = forbidden_generation
        sys.argv = [str(MODULE_PATH)]
    elif mode in {"single", "single-blocked"}:
        sys.argv = [str(MODULE_PATH), "--generate", "offline fixture",
                    "--output", str(root / "single.png"), "--account-id", "1"]
    else:
        sys.argv = [str(MODULE_PATH), "--generate-batch", str(root / "jobs.json"),
                    "--concurrency", "1"]
    try:
        asyncio.run(server.main())
    finally:
        (root / "handler-restored.json").write_text(json.dumps(
            {"restored": signal.getsignal(signal.SIGTERM) == original_handler}))


@unittest.skipUnless(os.name == "posix", "Subprocess signal delivery requires POSIX")
class CliSignalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow2api-cli-signal-")
        self.root = Path(self.temp.name)
        self.process = None

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.communicate()
        self.temp.cleanup()

    def start(self, mode="blocked", count=4):
        (self.root / "jobs.json").write_text(json.dumps([
            {"prompt": f"offline fixture {index}", "output_path": str(self.root / f"job-{index}.png")}
            for index in range(count)]))
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--fixture-child", str(self.root), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def wait_file(self, name):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists():
            if self.process.poll() is not None:
                output, errors = self.process.communicate()
                self.fail(f"Child exited before {name}: {self.process.returncode}; {output!r}; {errors!r}")
            if time.monotonic() >= deadline:
                self.fail(f"Child did not create {name}")
            time.sleep(0.005)

    def finish(self, code):
        output, errors = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, code, errors.decode())
        self.assertTrue(json.loads((self.root / "handler-restored.json").read_text())["restored"])
        return output

    def manifest(self):
        return json.loads(next(self.root.glob(".flow2api-batch-*.json")).read_text())

    def assert_interrupted_results(self):
        result = self.manifest()
        self.assertEqual(result["batch_state"], "cancelled")
        self.assertEqual(result["counts"], {"succeeded": 1, "failed": 1, "unknown": 1, "uncompleted": 1})
        self.assertEqual([job["status"] for job in result["jobs"]],
                         ["succeeded", "failed", "unknown", "uncompleted"])
        self.assertEqual(result["jobs"][1]["error"], "offline explicit failure")
        self.assertTrue(result["jobs"][2]["submission_attempted"])
        self.assertFalse(result["jobs"][3]["submission_attempted"])
        self.assertEqual(result["jobs"][3]["error"], "Not submitted: batch interrupted.")
        self.assertEqual((self.root / "job-0.png").read_bytes(), b"offline synthetic output")
        self.assertFalse((self.root / "job-2.png").exists())
        self.assertFalse((self.root / "job-3.png").exists())
        self.assertEqual(list(self.root.glob("*.flow2api-reserved")), [])
        self.assertEqual(list(self.root.glob(".flow2api-batch-*.tmp")), [])
        self.assertTrue((self.root / "cleanup-finished").is_file())

    def test_sigterm_finalizes_manifest_and_exits_143(self):
        self.start()
        self.wait_file("inflight-started")
        self.process.send_signal(signal.SIGTERM)
        self.finish(143)
        self.assert_interrupted_results()

    def test_repeated_sigterm_does_not_cancel_cleanup_again(self):
        self.start()
        self.wait_file("inflight-started")
        self.process.send_signal(signal.SIGTERM)
        self.wait_file("cleanup-started")
        self.process.send_signal(signal.SIGTERM)
        self.finish(143)
        self.assert_interrupted_results()

    def test_sigint_retains_asyncio_cancellation_behavior(self):
        self.start()
        self.wait_file("inflight-started")
        self.process.send_signal(signal.SIGINT)
        self.finish(-signal.SIGINT)
        self.assert_interrupted_results()

    def test_normal_success_preserves_result_and_restores_handler(self):
        self.start("success", count=2)
        result = json.loads(self.finish(0))
        self.assertTrue(result["success"])
        self.assertEqual(result["batch_state"], "completed")
        self.assertEqual(result["counts"]["succeeded"], 2)

    def test_normal_failed_batch_keeps_nonzero_result(self):
        self.start("failed", count=2)
        result = json.loads(self.finish(1))
        self.assertFalse(result["success"])
        self.assertEqual(result["batch_state"], "completed")
        self.assertEqual(result["counts"]["failed"], 1)

    def test_single_generation_remains_compatible(self):
        self.start("single")
        result = json.loads(self.finish(0))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["path"]).read_bytes(), b"offline synthetic output")

    def test_sigterm_also_cleans_single_generation_reservation(self):
        self.start("single-blocked")
        self.wait_file("inflight-started")
        self.process.send_signal(signal.SIGTERM)
        self.finish(143)
        self.assertTrue((self.root / "cleanup-finished").is_file())
        self.assertFalse((self.root / "single.png").exists())
        self.assertEqual(list(self.root.glob("*.flow2api-reserved")), [])

    def test_stdio_does_not_use_generation_signal_wrapper(self):
        self.start("stdio")
        self.assertEqual(self.finish(0), b"")
        self.assertEqual(list(self.root.glob(".flow2api-batch-*.json")), [])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--fixture-child":
        fixture_child(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
