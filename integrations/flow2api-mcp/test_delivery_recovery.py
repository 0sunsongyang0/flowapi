"""Offline media-only recovery tests; no generation or external traffic."""

import asyncio
import hashlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from test_server import server, fixture_png


class DeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow2api-delivery-offline-")
        self.root = Path(self.temp.name)
        self.cfg = {"base_url": "http://127.0.0.1:38000/v1", "api_key": "fixture-secret",
                    "media_proxy": "http://127.0.0.1:17900"}
        self.url = "https://media.fixture.test/image?signature=never-log-this"
        self.real_client = httpx.AsyncClient
        self.requests, self.options = [], []
        self.behaviors = []
        self.settings_patch = patch.object(server, "settings", return_value=self.cfg)
        self.settings_patch.start()
        self.client_patch = patch.object(server.httpx, "AsyncClient", side_effect=self.make_client)
        self.client_patch.start()

    async def asyncTearDown(self):
        self.client_patch.stop()
        self.settings_patch.stop()
        self.temp.cleanup()

    def make_client(self, **options):
        self.options.append(options)
        return self.real_client(transport=httpx.MockTransport(self.handle),
                                timeout=options["timeout"], follow_redirects=True)

    async def handle(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        self.assertNotIn("Authorization", request.headers)
        behavior = self.behaviors.pop(0) if self.behaviors else 200
        if behavior == "connect":
            raise httpx.ConnectError("fixture failure at " + self.url, request=request)
        if behavior == "slow":
            await asyncio.sleep(0.1)
        if isinstance(behavior, tuple):
            return httpx.Response(behavior[0], headers={"Retry-After": behavior[1]})
        return httpx.Response(behavior if isinstance(behavior, int) else 200, content=fixture_png())

    def manifest(self, **changes):
        batch_id = "c" * 32
        path = self.root / (".flow2api-batch-" + batch_id + ".json")
        job = {"job_index": 0, "status": "failed", "generation_completed": True,
               "image_url": self.url, "output_path": str(self.root / "image.png"),
               "account_id": 2, "error": "Image returned but could not be saved: ConnectError"}
        job.update(changes)
        path.write_text(json.dumps({"batch_id": batch_id, "batch_state": "completed", "jobs": [job]}))
        return batch_id, path, job

    async def test_connect_failure_retries_only_three_gets_with_tls_and_no_credentials(self):
        self.behaviors = ["connect", "connect", 200]
        with patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep:
            raw = await server.read_image(self.url, self.cfg)
        self.assertEqual(raw, fixture_png())
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])
        self.assertTrue(all(option["verify"] is True and option["trust_env"] is False
                            and option["proxy"] == self.cfg["media_proxy"] for option in self.options))

    async def test_get_attempt_count_is_bounded(self):
        self.behaviors = ["connect"] * 4
        with patch.object(server.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(httpx.ConnectError):
                await server.read_image(self.url, self.cfg)
        self.assertEqual(len(self.requests), 3)

    async def test_auth_statuses_are_not_retried(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.requests.clear()
                self.behaviors = [status]
                with self.assertRaises(httpx.HTTPStatusError):
                    await server.read_image(self.url, self.cfg)
                self.assertEqual(len(self.requests), 1)

    async def test_short_retry_after_is_respected_and_long_or_invalid_is_not_retried(self):
        self.behaviors = [(429, "1.25"), 200]
        with patch.object(server.asyncio, "sleep", new=AsyncMock()) as sleep:
            await server.read_image(self.url, self.cfg)
        sleep.assert_awaited_once_with(1.25)
        for delay in ("100", "nan", "-1", "tomorrow"):
            self.requests.clear()
            self.behaviors = [(429, delay)]
            with self.assertRaises(httpx.HTTPStatusError):
                await server.read_image(self.url, self.cfg)
            self.assertEqual(len(self.requests), 1)

    async def test_retry_and_stream_share_strict_total_deadline(self):
        self.cfg["job_timeout_seconds"] = 0.02
        self.behaviors = ["slow"]
        with self.assertRaises(TimeoutError):
            await server.read_image(self.url, self.cfg)
        self.assertEqual(len(self.requests), 1)

    async def test_recovery_creates_png_once_and_repeated_client_uses_receipt(self):
        batch_id, path, job = self.manifest()
        original = path.read_bytes()
        with patch.object(server, "submit_job", side_effect=AssertionError("No POST allowed")), \
                patch.object(server, "api_client", side_effect=AssertionError("No API requests allowed")):
            result, preview = await server.recover_image(batch_id, 0, str(self.root))
            repeated, _ = await server.recover_image(batch_id, 0, str(self.root))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(result["generation_replayed"])
        self.assertFalse(result["submission_attempted"])
        self.assertTrue(repeated["recovered_from_cache"])
        receipt = Path(result["recovery_manifest_path"])
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("never-log-this", receipt.read_text())
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(result["sha256"], hashlib.sha256(Path(job["output_path"]).read_bytes()).hexdigest())
        with Image.open(io.BytesIO(preview)) as image:
            self.assertEqual(image.size, (17, 13))

    async def test_recovery_never_overwrites_existing_output(self):
        batch_id, _, job = self.manifest()
        output = Path(job["output_path"])
        output.write_bytes(fixture_png(22))
        with self.assertRaisesRegex(ValueError, "already exists"):
            await server.recover_image(batch_id, 0, str(self.root))
        self.assertEqual(output.read_bytes(), fixture_png(22))
        self.assertEqual(self.requests, [])

    async def test_unknown_active_and_invalid_batch_id_are_rejected_before_get(self):
        batch_id, path, _ = self.manifest(outcome_unknown=True)
        with self.assertRaisesRegex(ValueError, "unknown"):
            await server.recover_image(batch_id, 0, str(self.root))
        batch_id, path, _ = self.manifest()
        manifest = json.loads(path.read_text())
        manifest["batch_state"] = "running"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "finished"):
            await server.recover_image(batch_id, 0, str(self.root))
        with self.assertRaisesRegex(ValueError, "identifier"):
            await server.recover_image("../" + batch_id, 0, str(self.root))
        self.assertEqual(self.requests, [])

    async def test_recovery_diagnostics_omit_signed_url_and_no_receipt_on_failure(self):
        batch_id, _, job = self.manifest()
        self.behaviors = ["connect"] * 3
        with patch.object(server.asyncio, "sleep", new=AsyncMock()):
            result = await server.call_tool("flow2api_recover_image", {
                "batch_id": batch_id, "job_index": 0, "manifest_directory": str(self.root)})
        self.assertTrue(result.isError)
        self.assertNotIn("never-log-this", result.content[0].text)
        self.assertIn("ConnectError", result.content[0].text)
        self.assertFalse(Path(job["output_path"]).exists())
        self.assertEqual(list(self.root.glob(".flow2api-recovery-*")), [])

    async def test_recovery_receipt_mismatch_refuses_another_download(self):
        batch_id, _, job = self.manifest()
        result, _ = await server.recover_image(batch_id, 0, str(self.root))
        Path(job["output_path"]).write_bytes(fixture_png(24))
        with self.assertRaisesRegex(ValueError, "changed"):
            await server.recover_image(batch_id, 0, str(self.root))
        self.assertEqual(len(self.requests), 1)

    async def test_execute_job_keeps_recoverable_url_but_diagnostic_does_not_leak_it(self):
        prepared = server.PreparedJob(0, "fixture", server.DEFAULT_MODEL, self.root / "failed.png", [])
        with patch.object(server, "submit_job", new=AsyncMock(return_value=(self.url, None, {}, None))), \
                patch.object(server, "save_image", side_effect=httpx.ConnectError("failure " + self.url)):
            result, preview = await server.execute_job(prepared, self.cfg, account_id=2)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["delivery_recoverable"])
        self.assertTrue(result["generation_completed"])
        self.assertEqual(result["image_url"], self.url)
        self.assertNotIn("never-log-this", result["error"])
        self.assertIsNone(preview)

    async def test_actual_recovery_cli_and_stdio_use_get_only_without_credentials(self):
        # Exercise the real subprocess CLI and MCP tool against a loopback media server.
        seen = []
        class MediaHandler(BaseHTTPRequestHandler):
            def do_GET(inner):
                seen.append((inner.command, inner.path, inner.headers.get("Authorization")))
                raw = fixture_png()
                inner.send_response(200)
                inner.send_header("Content-Length", str(len(raw)))
                inner.end_headers()
                inner.wfile.write(raw)
            def do_POST(inner):
                seen.append((inner.command, inner.path, None))
                inner.send_error(500)
            def log_message(inner, *_args):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            self.client_patch.stop()
            batch_id, path, _ = self.manifest(image_url=f"http://127.0.0.1:{http.server_port}/media")
            config = self.root / "settings.json"
            config.write_text(json.dumps(self.cfg))
            process = await asyncio.create_subprocess_exec(
                __import__("sys").executable, str(server.__file__), "--config", str(config),
                "--recover-batch", batch_id, "--job-index", "0", "--manifest-directory", str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, errors = await process.communicate()
            self.assertEqual(process.returncode, 0, errors.decode())
            self.assertTrue(json.loads(output)["success"])
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=__import__("sys").executable,
                args=[str(server.__file__), "--config", str(config)])
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool("flow2api_recover_image", {
                        "batch_id": batch_id, "job_index": 0, "manifest_directory": str(self.root)})
                    self.assertFalse(result.isError)
                    self.assertEqual([part.type for part in result.content], ["text", "image"])
                    self.assertTrue(json.loads(result.content[0].text)["recovered_from_cache"])
            self.assertEqual(seen, [("GET", "/media", None)])
        finally:
            self.client_patch.start()
            await asyncio.to_thread(http.shutdown)
            http.server_close()


if __name__ == "__main__":
    unittest.main()
