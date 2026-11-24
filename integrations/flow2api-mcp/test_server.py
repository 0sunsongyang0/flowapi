"""Offline MCP/CLI batch tests. Every image and API response is a local fixture."""

import asyncio
import base64
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


MODULE_PATH = Path(__file__).with_name("server.py")
spec = importlib.util.spec_from_file_location("flow2api_test_server", MODULE_PATH)
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
MODEL = server.DEFAULT_MODEL
OTHER = "gemini-test-image-portrait"


def fixture_png(width=17):
    buffer = io.BytesIO()
    Image.new("RGB", (width, 13), "teal").save(buffer, format="PNG")
    return buffer.getvalue()


def sse(events, done=True):
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return data + ("data: [DONE]\n\n" if done else "")


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow2api-batch-offline-")
        self.root = Path(self.temp.name)
        self.cfg = {"base_url": "http://fixture.test/v1", "api_key": "offline-test-key",
                    "output_directory": str(self.root / "default")}
        self.accounts = {MODEL: [{"id": 1, "concurrency_limit": 8}, {"id": 2, "concurrency_limit": 8}],
                         OTHER: [{"id": 2, "concurrency_limit": 8}]}
        self.posts, self.gets, self.active, self.peak = [], [], Counter(), Counter()
        self.started, self.finished, self.starts = Counter(), Counter(), []
        self.global_peak = 0
        self.behaviors = {}
        self.transport = httpx.MockTransport(self.handle)
        self.real_client = httpx.AsyncClient
        self.settings_patch = patch.object(server, "settings", return_value=self.cfg)
        self.client_patch = patch.object(server, "api_client", side_effect=lambda cfg: self.real_client(
            base_url=cfg["base_url"] + "/", headers={"Authorization": "Bearer " + cfg["api_key"]},
            transport=self.transport))
        self.settings_patch.start()
        self.client_patch.start()

    async def asyncTearDown(self):
        self.client_patch.stop()
        self.settings_patch.stop()
        self.temp.cleanup()

    def jobs(self, count):
        return [{"prompt": f"job {index}", "output_path": str(self.root / f"job-{index}.png")}
                for index in range(count)]

    async def handle(self, request):
        self.assertEqual(request.headers.get("Authorization"), "Bearer offline-test-key")
        if request.method == "GET":
            self.gets.append(str(request.url))
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": OTHER}]})
            if request.url.path == "/v1/image-accounts":
                model = request.url.params["model"]
                return httpx.Response(200, json={"accounts": self.accounts[model], "model": model,
                                                 "availability": "configured", "authenticated": False})
            raise AssertionError("Unexpected fixture GET " + str(request.url))
        self.assertEqual(request.url.path, "/v1/chat/completions")
        payload = json.loads(request.content)
        self.posts.append(payload)
        prompt = payload["messages"][0]["content"][0]["text"]
        account = payload.get("account_id", 0)
        self.active[account] += 1
        self.started[account] += 1
        self.starts.append({"account_id": account, "ordinal": self.started[account],
                            "previous_completed": self.finished[account]})
        self.peak[account] = max(self.peak[account], self.active[account])
        self.global_peak = max(self.global_peak, sum(self.active.values()))
        behavior = self.behaviors.get(prompt, "success")
        try:
            await asyncio.sleep(0.001 if behavior in {"unknown", "truncated", "auth", "auth_ineligible", "auth_string", "auth_unknown", "http401"} else 0.5 if behavior == "slow" else 0.025)
            if behavior == "connect":
                raise httpx.ConnectError("fixture connection refused", request=request)
            if behavior == "timeout":
                raise httpx.ReadTimeout("fixture read timed out after POST", request=request)
            if behavior == "http503":
                return httpx.Response(503, json={"detail": "fixture unavailable"})
            if behavior in {"not_sent_http", "not_sent_http_conflict"}:
                error = {"message": "fixture preparation queue expired", "submission_not_sent": True}
                if behavior == "not_sent_http_conflict":
                    error["outcome_unknown"] = True
                return httpx.Response(503, json={"error": error})
            if behavior == "http401":
                return httpx.Response(401, json={"detail": "fixture authentication rejected"})
            if behavior == "http403":
                return httpx.Response(403, json={"detail": "fixture content denied"})
            if behavior in {"not_sent", "not_sent_alias", "not_sent_conflict", "not_sent_text"}:
                error = {"message": "Flow browser preparation queue timed out; no generation request submitted"}
                if behavior == "not_sent":
                    error["submission_not_sent"] = True
                elif behavior == "not_sent_alias":
                    error["generation_submitted"] = False
                elif behavior == "not_sent_conflict":
                    error.update(submission_not_sent=True, outcome_unknown=True)
                body = sse([{"error": error}])
            elif behavior in {"auth", "auth_ineligible", "auth_string", "auth_unknown"}:
                message = (f"指定账号 #{account} 当前不可用于该模型，或登录验证失败" if behavior == "auth_ineligible"
                           else f"指定账号 #{account} 生成失败: Flow 登录认证失败：Google Cookie 已失效、不完整，或来自不同账号 (rpc=ogiZ0b, code=401)")
                error = message if behavior == "auth_string" else {"message": message}
                if behavior == "auth_unknown":
                    error["outcome_unknown"] = True
                body = sse([{"error": error}])
            elif behavior in {"failed", "unknown"}:
                error = {"message": "fixture rejected"}
                if behavior == "unknown":
                    error["outcome_unknown"] = True
                body = sse([{"error": error}])
            elif behavior == "truncated":
                body = sse([{"choices": [{"delta": {"content": "Generating"}}]}], done=False)
            elif behavior == "noimage":
                body = sse([{"choices": [{"delta": {"content": "No image"}}]}])
            else:
                width = int(prompt.split()[-1]) + 17 if prompt.startswith("job ") else 17
                raw = b"not an image" if behavior == "badimage" else fixture_png(width)
                url = "data:image/png;base64," + base64.b64encode(raw).decode()
                text = "![result](" + url + ")"
                tail = {"choices": [{"delta": {"content": text[20:]}}]}
                marker = {"upscaled_image": {"outcome_unknown": True, "submission_uncertain": True}}
                if behavior == "upscale_unknown":
                    tail["metadata"] = {"generated_assets": marker}
                elif behavior == "upscale_unknown_top":
                    tail["generated_assets"] = marker
                events = [{"choices": [{"delta": {"content": text[:20]}}]}, tail]
                if behavior in {"failure_after_image", "unknown_after_image", "auth_after_image", "not_sent_after_image"}:
                    message = "Flow 登录认证失败：fixture rejected after image" if behavior == "auth_after_image" else "fixture error after image"
                    error = {"message": message, "outcome_unknown": behavior == "unknown_after_image"}
                    if behavior == "not_sent_after_image":
                        error["submission_not_sent"] = True
                    events.append({"error": error})
                body = sse(events, done=behavior != "truncated_after_image")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        finally:
            self.active[account] -= 1
            self.finished[account] += 1

    def assert_no_posts(self):
        self.assertEqual(self.posts, [])
        self.assertEqual(list(self.root.rglob("*.flow2api-reserved")), [])

    async def test_all_models_preflight_before_first_submission(self):
        jobs = self.jobs(2)
        jobs[1]["model"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown image model"):
            await server.generate_images(jobs)
        self.assert_no_posts()

    async def test_all_references_preflight_before_first_submission(self):
        jobs = self.jobs(2)
        jobs[1]["reference_image_paths"] = [str(self.root / "missing.png")]
        with self.assertRaisesRegex(ValueError, "existing absolute"):
            await server.generate_images(jobs)
        self.assert_no_posts()

    async def test_duplicate_resolved_output_and_existing_output_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        jobs = self.jobs(2)
        jobs[1]["output_path"] = str(alias / "job-0.png")
        with self.assertRaisesRegex(ValueError, "duplicate output"):
            await server.generate_images(jobs)
        (self.root / "job-1.png").write_bytes(fixture_png())
        with self.assertRaisesRegex(ValueError, "already exists"):
            await server.generate_images(self.jobs(2))
        self.assert_no_posts()

    async def test_invalid_late_output_parent_rejected_before_submission(self):
        blocker = self.root / "file"
        blocker.write_text("not a directory")
        jobs = self.jobs(2)
        jobs[1]["output_path"] = str(blocker / "nested" / "output.png")
        with self.assertRaises(ValueError):
            await server.generate_images(jobs)
        self.assert_no_posts()

    async def test_invalid_job_fields_and_count_are_preflight_only(self):
        for jobs in ([], self.jobs(33), [{"prompt": "x", "n": 5}],
                     [{"prompt": "x", "reference_image_paths": "file.png"}],
                     [{"prompt": " "}], [{"prompt": "x", "output_path": "relative.png"}]):
            with self.subTest(jobs_type=type(jobs), count=len(jobs)):
                with self.assertRaises(ValueError):
                    await server.generate_images(jobs)
                self.assert_no_posts()

    async def test_no_eligible_account_aborts_all_jobs_before_submission(self):
        self.accounts[OTHER] = []
        jobs = self.jobs(2)
        jobs[1]["model"] = OTHER
        with self.assertRaisesRegex(ValueError, "No configured eligible"):
            await server.generate_images(jobs)
        self.assert_no_posts()

    async def test_nineteen_jobs_queue_over_two_accounts_at_eight_each(self):
        result, previews = await server.generate_images(self.jobs(19))
        self.assertTrue(result["success"])
        self.assertEqual(result["concurrency"], 16)
        self.assertEqual(self.global_peak, 16)
        self.assertEqual(self.peak, {1: 8, 2: 8})
        self.assertEqual([payload["account_id"] for payload in self.posts[:2]], [1, 2])
        for account_id in (1, 2):
            ninth = next(start for start in self.starts if start["account_id"] == account_id and start["ordinal"] == 9)
            self.assertGreaterEqual(ninth["previous_completed"], 1, "Ninth request must wait for an account slot.")
        self.assertEqual(len(self.posts), 19)
        self.assertEqual(len(previews), 19)
        self.assertEqual(len({job["path"] for job in result["jobs"]}), 19)
        for index, job in enumerate(result["jobs"]):
            self.assertEqual((job["job_index"], job["status"], job["width"]), (index, "succeeded", index + 17))
            with Image.open(job["path"]) as image:
                self.assertEqual(image.size, (index + 17, 13))
        self.assertEqual(len([url for url in self.gets if "image-accounts" in url]), 1)
        self.assertEqual(list(self.root.rglob("*.flow2api-reserved")), [])

    async def test_each_job_preserves_model_reference_prompt_and_output(self):
        self.accounts[MODEL] = [{"id": 1, "concurrency_limit": 8}]
        ref = self.root / "reference.png"
        ref.write_bytes(fixture_png())
        jobs = self.jobs(3)
        jobs[0].update(reference_image_paths=[str(ref)])
        jobs[1].update(model=OTHER)
        result, _ = await server.generate_images(jobs)
        self.assertTrue(result["success"])
        self.assertEqual([payload["account_id"] for payload in self.posts], [1, 2, 1])
        self.assertEqual(self.posts[1]["model"], OTHER)
        self.assertEqual(len(self.posts[0]["messages"][0]["content"]), 2)
        self.assertEqual(len(self.posts[1]["messages"][0]["content"]), 1)
        self.assertEqual([job["path"] for job in result["jobs"]], [job["output_path"] for job in jobs])

    async def test_returned_account_capacities_and_optional_global_cap(self):
        self.accounts[MODEL] = [{"id": 1, "concurrency_limit": 2}, {"id": 2, "concurrency_limit": 1}]
        result, _ = await server.generate_images(self.jobs(6))
        self.assertEqual(result["concurrency"], 3)
        self.assertEqual(self.peak, {1: 2, 2: 1})
        self.assertEqual(self.global_peak, 3)
        self.assertEqual(result["per_account_limits"], [{"account_id": 1, "concurrency_limit": 2},
                                                       {"account_id": 2, "concurrency_limit": 1}])
        self.posts.clear()
        self.global_peak = 0
        other = [{"prompt": f"other {i}", "output_path": str(self.root / f"other-{i}.png")} for i in range(4)]
        result, _ = await server.generate_images(other, concurrency=2)
        self.assertEqual(result["concurrency"], 2)
        self.assertEqual(self.global_peak, 2)

    async def test_explicit_concurrency_cannot_exceed_account_or_config_capacity(self):
        for value in (17, 0, True, "2"):
            with self.assertRaises(ValueError):
                await server.generate_images(self.jobs(2), concurrency=value)
        self.cfg["batch_max_concurrency"] = 2
        with self.assertRaises(ValueError):
            await server.generate_images(self.jobs(2), concurrency=3)
        self.assert_no_posts()

    async def test_legacy_accounts_without_capacity_default_to_eight(self):
        self.accounts[MODEL] = [{"id": 1}]
        result, _ = await server.generate_images(self.jobs(9))
        self.assertEqual(result["concurrency"], 8)
        self.assertEqual(self.peak, {1: 8})

    async def test_reported_capacity_above_eight_is_capped(self):
        self.accounts[MODEL] = [{"id": 1, "concurrency_limit": 99}]
        result, _ = await server.generate_images(self.jobs(9))
        self.assertEqual(result["concurrency"], 8)
        self.assertEqual(result["per_account_limits"], [{"account_id": 1, "concurrency_limit": 8}])
        self.assertEqual(self.peak, {1: 8})

    async def test_known_failure_preserves_successes_and_continues_queue(self):
        self.behaviors["job 0"] = "failed"
        result, previews = await server.generate_images(self.jobs(3), concurrency=1)
        self.assertFalse(result["success"])
        self.assertEqual(result["counts"], {"succeeded": 2, "failed": 1, "unknown": 0, "uncompleted": 0})
        self.assertEqual(set(previews), {1, 2})
        self.assertEqual(len(self.posts), 3)
        self.assertFalse((self.root / "job-0.png").exists())

    def test_account_rejection_classifier_only_matches_known_backend_messages(self):
        for message in ("Flow 登录认证失败", "Flow 登录认证失败：所选 Google 身份未通过验证，请重新同步账号",
                        "指定账号 #1 生成失败: Flow 登录认证失败：Google Cookie 已失效 (code=401)",
                        "指定账号 #2 当前不可用于该模型，或登录验证失败"):
            with self.subTest(message=message):
                self.assertTrue(server.is_explicit_account_rejection(message))
        for message in (None, "", "401", "fixture returned code=401", "Browser captcha queue wait timeout",
                        "Flow browser preparation timed out after 120s", "No native reCAPTCHA runtime",
                        "Recaptcha validation failed", "public_error_unusual_activity", "public_error_user_quota_reached",
                        "Image policy rejected", "Prompt mentions Flow 登录认证失败：a fictional caption"):
            with self.subTest(message=message):
                self.assertFalse(server.is_explicit_account_rejection(message))

    async def test_explicit_account_rejection_stops_queue_but_drains_active_jobs_without_failover(self):
        self.behaviors["job 0"] = "auth"
        self.behaviors["job 1"] = "slow"
        task = asyncio.create_task(server.generate_images(self.jobs(4), concurrency=2))

        async def rejection_manifest():
            while True:
                for path in self.root.glob(".flow2api-batch-*.json"):
                    manifest = json.loads(path.read_text())
                    if manifest.get("pause_reason", {}).get("code") == "account_rejected":
                        return manifest
                await asyncio.sleep(0.001)

        during = await asyncio.wait_for(rejection_manifest(), 1)
        self.assertEqual([job["status"] for job in during["jobs"]],
                         ["failed", "running", "uncompleted", "uncompleted"])
        self.assertEqual(len(self.posts), 2)
        result, previews = await task
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["batch_state"], "paused")
        self.assertEqual(result["counts"], {"succeeded": 1, "failed": 1, "unknown": 0, "uncompleted": 2})
        self.assertEqual(result["pause_reason"]["code"], "account_rejected")
        self.assertEqual(result["pause_reason"]["account_id"], 1)
        self.assertIn("Flow 登录认证失败", result["pause_reason"]["error"])
        self.assertEqual([payload["account_id"] for payload in self.posts], [1, 2])
        self.assertEqual([payload["messages"][0]["content"][0]["text"] for payload in self.posts], ["job 0", "job 1"])
        self.assertEqual(set(previews), {1})
        self.assertEqual(sum(self.active.values()), 0)
        self.assertTrue(Path(result["jobs"][1]["path"]).is_file())
        for job in result["jobs"][2:]:
            self.assertFalse(job["submission_attempted"])
            self.assertIn("account authentication or model eligibility", job["error"])
        final = json.loads(Path(result["manifest_path"]).read_text())
        self.assertEqual(final["pause_reason"], result["pause_reason"])
        self.assertEqual(final["counts"], result["counts"])

    async def test_each_trusted_rejection_form_preserves_unstarted_jobs_without_retry(self):
        for behavior in ("auth", "auth_ineligible", "auth_string", "http401", "auth_unknown"):
            with self.subTest(behavior=behavior):
                self.behaviors[behavior] = behavior
                self.posts.clear()
                jobs = [{"prompt": behavior, "output_path": str(self.root / (behavior + ".png"))},
                        {"prompt": "never submitted", "output_path": str(self.root / (behavior + "-later.png"))}]
                result, _ = await server.generate_images(jobs, concurrency=1)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(result["batch_state"], "paused")
                self.assertTrue(result["jobs"][0]["account_rejected"])
                self.assertEqual(result["jobs"][0]["status"], "unknown" if behavior == "auth_unknown" else "failed")
                self.assertEqual(result["jobs"][1]["status"], "uncompleted")
                self.assertFalse(result["jobs"][1]["submission_attempted"])
                self.assertEqual(result["pause_reason"]["code"], "account_rejected")

    async def test_account_rejection_remains_paused_when_every_job_already_started(self):
        self.behaviors["job 0"] = "auth"
        result, _ = await server.generate_images(self.jobs(2), concurrency=2)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(result["counts"], {"succeeded": 1, "failed": 1, "unknown": 0, "uncompleted": 0})
        self.assertEqual(result["batch_state"], "paused")
        self.assertEqual(result["pause_reason"]["code"], "account_rejected")
        self.assertTrue(result["jobs"][0]["account_rejected"])

    async def test_content_http403_does_not_imply_account_rejection(self):
        self.behaviors["job 0"] = "http403"
        result, _ = await server.generate_images(self.jobs(2), concurrency=1)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(result["batch_state"], "completed")
        self.assertNotIn("pause_reason", result)
        self.assertNotIn("account_rejected", result["jobs"][0])
        self.assertEqual(result["counts"]["succeeded"], 1)

    async def test_account_rejection_after_image_retains_file_and_preview_and_pauses_queue(self):
        self.behaviors["job 0"] = "auth_after_image"
        result, previews = await server.generate_images(self.jobs(2), concurrency=1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(result["jobs"][0]["status"], "failed")
        self.assertTrue(result["jobs"][0]["account_rejected"])
        self.assertTrue(Path(result["jobs"][0]["path"]).is_file())
        self.assertIn(0, previews)
        self.assertEqual(result["jobs"][1]["status"], "uncompleted")
        self.assertEqual(result["pause_reason"]["code"], "account_rejected")

    async def test_unknown_stops_unstarted_jobs_and_never_replays(self):
        self.behaviors["job 0"] = "unknown"
        result, previews = await server.generate_images(self.jobs(4), concurrency=2)
        self.assertEqual(result["counts"], {"succeeded": 1, "failed": 0, "unknown": 1, "uncompleted": 2})
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(set(previews), {1})
        self.assertTrue(result["jobs"][0]["submission_attempted"])
        self.assertFalse(result["jobs"][2]["submission_attempted"])

    async def test_stream_truncation_timeout_and_5xx_are_unknown_without_retry(self):
        for behavior in ("truncated", "timeout", "http503"):
            with self.subTest(behavior=behavior):
                self.behaviors["job 0"] = behavior
                self.posts.clear()
                result, _ = await server.generate_images(self.jobs(2), concurrency=1)
                self.assertEqual(result["counts"]["unknown"], 1)
                self.assertEqual(result["counts"]["uncompleted"], 1)
                self.assertEqual(len(self.posts), 1)

    async def test_structured_not_sent_is_failed_without_unknown_or_automatic_retry(self):
        for behavior in ("not_sent", "not_sent_alias", "not_sent_http"):
            with self.subTest(behavior=behavior):
                self.behaviors[behavior] = behavior
                self.posts.clear()
                jobs = [{"prompt": behavior, "output_path": str(self.root / (behavior + ".png"))},
                        {"prompt": "later", "output_path": str(self.root / (behavior + "-later.png"))}]
                result, _ = await server.generate_images(jobs, concurrency=1)
                failed = result["jobs"][0]
                self.assertEqual(failed["status"], "failed")
                self.assertFalse(failed["submission_attempted"])
                self.assertTrue(failed["submission_not_sent"])
                self.assertEqual(result["counts"]["unknown"], 0)
                self.assertEqual(result["counts"]["succeeded"], 1)
                self.assertEqual(len(self.posts), 2)
                self.assertEqual(sum(payload["messages"][0]["content"][0]["text"] == behavior
                                     for payload in self.posts), 1)

    async def test_not_sent_message_alone_does_not_assert_structured_submission_state(self):
        self.behaviors["job 0"] = "not_sent_text"
        result, _ = await server.generate_images(self.jobs(1))
        self.assertEqual(result["jobs"][0]["status"], "failed")
        self.assertTrue(result["jobs"][0]["submission_attempted"])
        self.assertNotIn("submission_not_sent", result["jobs"][0])

    async def test_unknown_marker_wins_over_conflicting_not_sent_and_pauses_without_retry(self):
        for behavior in ("not_sent_conflict", "not_sent_http_conflict"):
            with self.subTest(behavior=behavior):
                self.behaviors[behavior] = behavior
                self.posts.clear()
                jobs = [{"prompt": behavior, "output_path": str(self.root / (behavior + ".png"))},
                        {"prompt": "later", "output_path": str(self.root / (behavior + "-later.png"))}]
                result, _ = await server.generate_images(jobs, concurrency=1)
                self.assertEqual(result["jobs"][0]["status"], "unknown")
                self.assertTrue(result["jobs"][0]["submission_attempted"])
                self.assertNotIn("submission_not_sent", result["jobs"][0])
                self.assertEqual(result["jobs"][1]["status"], "uncompleted")
                self.assertFalse(result["jobs"][1]["submission_attempted"])
                self.assertEqual(result["pause_reason"]["code"], "outcome_unknown")
                self.assertEqual(len(self.posts), 1)

    async def test_job_timeout_setting_is_validated_before_any_request(self):
        self.assertEqual(server.job_timeout_seconds({}), 1800.0)
        for value in (0.01, 1, 1800, 3600):
            with self.subTest(valid=value):
                self.assertEqual(server.job_timeout_seconds({"job_timeout_seconds": value}), value)
        for value in (None, True, False, "1800", 0, -1, float("nan"), float("inf"),
                      float("-inf"), 3600.01, 10 ** 1000):
            with self.subTest(invalid=repr(value)):
                self.cfg["job_timeout_seconds"] = value
                with self.assertRaisesRegex(ValueError, "job_timeout_seconds"):
                    await server.generate_images(self.jobs(1))
                self.assertEqual(self.posts, [])
                self.assertEqual(self.gets, [])

    async def test_http_read_write_and_pool_use_job_budget_but_connection_stays_30(self):
        self.cfg["job_timeout_seconds"] = 999
        # Bypass the fixture's api_client patch to inspect the production timeout contract.
        self.client_patch.stop()
        try:
            async with server.api_client(self.cfg) as client:
                self.assertEqual(client.timeout.read, 999)
                self.assertEqual(client.timeout.write, 999)
                self.assertEqual(client.timeout.pool, 999)
                self.assertEqual(client.timeout.connect, 30)
        finally:
            self.client_patch.start()

    async def test_actual_job_deadline_is_unknown_and_pauses_queue_without_retry(self):
        self.cfg["job_timeout_seconds"] = 0.01
        self.behaviors["job 0"] = "slow"
        result, _ = await server.generate_images(self.jobs(2), concurrency=1)
        self.assertEqual(result["jobs"][0]["status"], "unknown")
        self.assertTrue(result["jobs"][0]["submission_attempted"])
        self.assertEqual(result["jobs"][1]["status"], "uncompleted")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(sum(self.active.values()), 0)

    async def test_generation_and_download_share_one_total_job_deadline(self):
        self.cfg["job_timeout_seconds"] = 0.25
        original_submit = server.submit_job
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed_submit(*args, **kwargs):
            await asyncio.sleep(0.075)
            return await original_submit(*args, **kwargs)

        async def stalled_save(*args, **kwargs):
            entered.set()
            try:
                # This stage fits a fresh budget, but not the budget remaining after generation.
                await asyncio.sleep(0.2)
                return {"success": True}, b"fixture preview"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch.object(server, "submit_job", side_effect=delayed_submit), \
                patch.object(server, "save_image", side_effect=stalled_save):
            result, _ = await server.generate_images(self.jobs(1))
        failed = result["jobs"][0]
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["generation_completed"])
        self.assertIn("image_url", failed)
        self.assertIn("TimeoutError", failed["error"])
        self.assertNotIn("outcome_unknown", failed)
        self.assertEqual(len(self.posts), 1)

    async def test_not_sent_marker_after_image_does_not_erase_generation_or_trigger_replay(self):
        self.behaviors["job 0"] = "not_sent_after_image"
        result, previews = await server.generate_images(self.jobs(1))
        job = result["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["submission_attempted"])
        self.assertTrue(job["generation_completed"])
        self.assertNotIn("submission_not_sent", job)
        self.assertIn("image_url", job)
        self.assertTrue(Path(job["path"]).is_file())
        self.assertIn(0, previews)
        self.assertEqual(len(self.posts), 1)

    async def test_rejected_connection_is_failed_not_unknown(self):
        self.behaviors["job 0"] = "connect"
        result, _ = await server.generate_images(self.jobs(1))
        self.assertEqual(result["jobs"][0]["status"], "failed")
        self.assertFalse(result["jobs"][0]["submission_attempted"])
        self.assertEqual(len(self.posts), 1)

    async def test_save_failure_preserves_generated_url_without_resubmission(self):
        self.behaviors["job 0"] = "badimage"
        result, _ = await server.generate_images(self.jobs(2), concurrency=1)
        failed = result["jobs"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["generation_completed"])
        self.assertTrue(failed["image_url"].startswith("data:image/"))
        self.assertEqual(len(self.posts), 2)
        self.assertFalse((self.root / "job-0.png").exists())

    async def test_single_tool_remains_compatible_and_pin_is_optional(self):
        result, preview = await server.generate_image("legacy", output_path=str(self.root / "legacy.png"))
        self.assertTrue(result["success"])
        self.assertTrue(preview)
        self.assertNotIn("account_id", self.posts[-1])
        self.assertEqual(self.gets, ["http://fixture.test/v1/models"])
        result, _ = await server.generate_image("pinned", output_path=str(self.root / "pinned.png"), account_id=2)
        self.assertEqual(result["account_id"], 2)
        self.assertEqual(self.posts[-1]["account_id"], 2)
        count = len(self.posts)
        with self.assertRaises(ValueError):
            await server.generate_image("invalid pin", account_id=99)
        self.assertEqual(len(self.posts), count)

    async def test_duplicate_concurrent_calls_reserve_before_generation(self):
        first = asyncio.create_task(server.generate_image("first", output_path=str(self.root / "same.png")))
        await asyncio.sleep(0.005)
        with self.assertRaisesRegex(ValueError, "reserved"):
            await server.generate_image("second", output_path=str(self.root / "same.png"))
        await first
        self.assertEqual(len(self.posts), 1)

    async def test_mcp_discovery_and_preview_association(self):
        tools = await server.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"flow2api_check_connection", "flow2api_list_image_models",
            "flow2api_list_image_accounts", "flow2api_generate_image", "flow2api_generate_images", "flow2api_recover_image"})
        result = await server.call_tool("flow2api_generate_images", {"jobs": self.jobs(2)})
        self.assertFalse(result.isError)
        self.assertEqual([part.type for part in result.content], ["text", "text", "image", "text", "image"])
        self.assertEqual(json.loads(result.content[1].text)["preview_for_job_index"], 0)
        self.assertEqual(json.loads(result.content[3].text)["preview_for_job_index"], 1)
        for part in (result.content[2], result.content[4]):
            with Image.open(io.BytesIO(base64.b64decode(part.data))) as image:
                self.assertEqual(image.format, "PNG")

    async def test_upscale_unknown_retains_original_and_pauses_queued_jobs(self):
        self.behaviors["job 0"] = "upscale_unknown"
        result, previews = await server.generate_images(self.jobs(2), concurrency=1)
        self.assertFalse(result["success"])
        job = result["jobs"][0]
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["upscale_status"], "unknown")
        self.assertTrue(job["generated_assets"]["upscaled_image"]["outcome_unknown"])
        self.assertTrue(Path(job["path"]).is_file())
        self.assertIn(0, previews)
        self.assertEqual(result["jobs"][1]["status"], "uncompleted")
        self.assertEqual(len(self.posts), 1)

    async def test_cancelled_batch_cleans_workers_and_output_reservations(self):
        task = asyncio.create_task(server.generate_images(self.jobs(3), concurrency=2))
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sum(self.active.values()), 0)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(list(self.root.rglob("*.flow2api-reserved")), [])
        manifest = json.loads(next(self.root.glob(".flow2api-batch-*.json")).read_text())
        self.assertEqual(manifest["batch_state"], "cancelled")
        self.assertEqual(manifest["counts"], {"succeeded": 0, "failed": 0, "unknown": 2, "uncompleted": 1})

    async def test_manifest_preserves_completed_job_when_later_job_is_cancelled(self):
        self.behaviors["job 1"] = "slow"
        reference = self.root / "reference.png"
        reference.write_bytes(fixture_png())
        jobs = self.jobs(3)
        jobs[0]["reference_image_paths"] = [str(reference)]
        task = asyncio.create_task(server.generate_images(jobs, concurrency=1))

        async def second_started():
            while len(self.posts) < 2:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(second_started(), 1)
        self.assertTrue((self.root / "job-0.png").is_file())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        path = next(self.root.glob(".flow2api-batch-*.json"))
        manifest_text = path.read_text()
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["batch_state"], "cancelled")
        self.assertEqual([job["status"] for job in manifest["jobs"]], ["succeeded", "unknown", "uncompleted"])
        self.assertEqual(manifest["jobs"][0]["path"], str(self.root / "job-0.png"))
        self.assertEqual(manifest["jobs"][1]["account_id"], 2)
        self.assertTrue(manifest["jobs"][1]["submission_attempted"])
        self.assertFalse(manifest["jobs"][2]["submission_attempted"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("offline-test-key", manifest_text)
        self.assertNotIn("data:image", manifest_text)
        self.assertNotIn("reference_image_paths", manifest_text)
        self.assertEqual(sum(self.active.values()), 0)

    async def test_success_returns_atomic_private_manifest_with_job_results(self):
        result, _ = await server.generate_images(self.jobs(2))
        path = Path(result["manifest_path"])
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest["batch_id"], result["batch_id"])
        self.assertEqual(manifest["batch_state"], "completed")
        self.assertEqual(manifest["counts"], result["counts"])
        self.assertEqual([job["path"] for job in manifest["jobs"]], [job["path"] for job in result["jobs"]])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".flow2api-batch-*.tmp")), [])

    async def test_top_level_upscale_marker_is_preserved(self):
        self.behaviors["job 0"] = "upscale_unknown_top"
        result, _ = await server.generate_images(self.jobs(1))
        self.assertEqual(result["jobs"][0]["upscale_status"], "unknown")
        self.assertTrue(result["jobs"][0]["generated_assets"]["upscaled_image"]["outcome_unknown"])

    async def test_error_after_image_preserves_url_file_preview_and_does_not_retry(self):
        for behavior in ("failure_after_image", "unknown_after_image", "truncated_after_image"):
            with self.subTest(behavior=behavior):
                self.behaviors[behavior] = behavior
                self.posts.clear()
                jobs = [{"prompt": behavior, "output_path": str(self.root / (behavior + ".png"))},
                        {"prompt": "later", "output_path": str(self.root / (behavior + "-later.png"))}]
                result, previews = await server.generate_images(jobs, concurrency=1)
                job = result["jobs"][0]
                self.assertTrue(Path(job["path"]).is_file())
                self.assertIn(0, previews)
                self.assertIn("image_url", job)
                self.assertIn("post_image_error", job)
                if behavior == "failure_after_image":
                    self.assertEqual(job["status"], "failed")
                    self.assertEqual(len(self.posts), 2)
                else:
                    self.assertTrue(job["outcome_unknown"])
                    self.assertEqual(result["jobs"][1]["status"], "uncompleted")
                    self.assertEqual(len(self.posts), 1)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual stdio and CLI entry points against loopback HTTP only."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="flow2api-protocol-offline-")
        self.root = Path(self.temp.name)
        self.posts = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, content_type="application/json"):
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self.reply({"data": [{"id": MODEL}]})
                elif self.path == "/health":
                    self.reply({"active_tokens": 2})
                elif self.path.startswith("/v1/image-accounts?"):
                    self.reply({"accounts": [{"id": 1, "concurrency_limit": 8},
                                             {"id": 2, "concurrency_limit": 8}],
                                "availability": "configured", "authenticated": False})
                else:
                    self.send_error(404)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.posts.append(payload)
                prompt = payload["messages"][0]["content"][0]["text"]
                if prompt == "explicit failure":
                    body = sse([{"error": {"message": "local fixture failed"}}])
                elif prompt == "explicit account rejection":
                    body = sse([{"error": {"message": "指定账号 #1 生成失败: Flow 登录认证失败：fixture cookie invalid"}}])
                else:
                    url = "data:image/png;base64," + base64.b64encode(fixture_png()).decode()
                    body = sse([{"choices": [{"delta": {"content": "![fixture](" + url + ")"}}]}])
                self.reply(body.encode(), "text/event-stream")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.config = self.root / "settings.json"
        self.config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{self.http.server_port}/v1",
                                           "api_key": "local-fixture-key", "output_directory": str(self.root)}))

    async def asyncTearDown(self):
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        self.temp.cleanup()

    async def cli(self, *args):
        process = await asyncio.create_subprocess_exec(sys.executable, str(MODULE_PATH), "--config", str(self.config),
                                                       *args, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        output, errors = await process.communicate()
        return process.returncode, output.decode(), errors.decode()

    async def test_actual_stdio_protocol_batch_previews_and_single_compatibility(self):
        params = StdioServerParameters(command=sys.executable, args=[str(MODULE_PATH), "--config", str(self.config)])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                self.assertEqual(len((await client.list_tools()).tools), 6)
                check = await client.call_tool("flow2api_check_connection", {})
                self.assertFalse(check.isError)
                accounts = await client.call_tool("flow2api_list_image_accounts", {})
                self.assertEqual(len(json.loads(accounts.content[0].text)["accounts"]), 2)
                jobs = [{"prompt": "first", "output_path": str(self.root / "batch-a.png")},
                        {"prompt": "second", "output_path": str(self.root / "batch-b.png")}]
                batch = await client.call_tool("flow2api_generate_images", {"jobs": jobs})
                self.assertFalse(batch.isError)
                self.assertEqual([part.type for part in batch.content], ["text", "text", "image", "text", "image"])
                self.assertEqual(json.loads(batch.content[0].text)["counts"]["succeeded"], 2)
                single = await client.call_tool("flow2api_generate_image", {
                    "prompt": "single", "output_path": str(self.root / "single.png")})
                self.assertFalse(single.isError)
                self.assertEqual([part.type for part in single.content], ["text", "image"])
        self.assertEqual(len(self.posts), 3)
        self.assertEqual({payload["account_id"] for payload in self.posts[:2]}, {1, 2})
        self.assertNotIn("account_id", self.posts[2])

    async def test_actual_cli_accounts_batch_and_partial_failure_exit_code(self):
        code, output, errors = await self.cli("--list-accounts")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(json.loads(output)["accounts"]), 2)
        self.assertEqual(self.posts, [])
        jobs_file = self.root / "jobs.json"
        jobs_file.write_text(json.dumps([{"prompt": "one", "output_path": str(self.root / "one.png")},
                                         {"prompt": "two", "output_path": str(self.root / "two.png")},
                                         {"prompt": "explicit failure", "output_path": str(self.root / "failed.png")}]))
        code, output, errors = await self.cli("--generate-batch", str(jobs_file), "--concurrency", "2")
        self.assertEqual(code, 1, errors)
        result = json.loads(output)
        self.assertEqual(result["counts"], {"succeeded": 2, "failed": 1, "unknown": 0, "uncompleted": 0})
        self.assertTrue((self.root / "one.png").is_file())
        self.assertFalse((self.root / "failed.png").exists())
        self.assertEqual(len(self.posts), 3)

    async def test_actual_cli_account_rejection_returns_partial_nonzero_and_unsubmitted_tail(self):
        jobs_file = self.root / "auth-jobs.json"
        jobs_file.write_text(json.dumps([{"prompt": "one", "output_path": str(self.root / "one.png")},
                                         {"prompt": "explicit account rejection", "output_path": str(self.root / "rejected.png")},
                                         {"prompt": "never submitted", "output_path": str(self.root / "tail.png")}]))
        code, output, errors = await self.cli("--generate-batch", str(jobs_file), "--concurrency", "1")
        self.assertEqual(code, 1, errors)
        result = json.loads(output)
        self.assertEqual(len(self.posts), 2)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["batch_state"], "paused")
        self.assertEqual(result["pause_reason"]["code"], "account_rejected")
        self.assertEqual(result["pause_reason"]["job_index"], 1)
        self.assertEqual(result["counts"], {"succeeded": 1, "failed": 1, "unknown": 0, "uncompleted": 1})
        self.assertTrue((self.root / "one.png").is_file())
        self.assertFalse((self.root / "tail.png").exists())
        self.assertFalse(result["jobs"][2]["submission_attempted"])


if __name__ == "__main__":
    unittest.main()
