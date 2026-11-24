"""Local image generation tools for Antigravity, backed by Flow2API."""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import os
import re
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from PIL import Image


app = Server("flow2api", version="1.1.0", instructions=(
    "Use flow2api_generate_image to create images through the user's local Google Flow account. "
    "Provide an absolute output_path within the current project when possible. "
    "One call generates one image and may consume the user's Flow credits. "
    "Use flow2api_generate_images for several independent jobs; each job generates one image. "
    "Batch jobs are pinned to eligible accounts and respect each account's concurrency limit (at most eight). "
    "Unknown submissions and explicit account authentication rejections pause queued jobs; no automatic replay or failover. "
    "Use the returned saved file and image preview; never invent a successful result."
))
CONFIG_PATH = Path(__file__).with_name("settings.json")
MAX_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
DEFAULT_MODEL = "gemini-3.1-flash-image-landscape"
MAX_BATCH_JOBS = 32
MAX_ACCOUNT_CONCURRENCY = 8
DEFAULT_JOB_TIMEOUT_SECONDS = 1800.0
MAX_JOB_TIMEOUT_SECONDS = 3600.0
MEDIA_GET_ATTEMPTS = 3
MEDIA_GET_TIMEOUT_SECONDS = 60.0


@dataclass
class PreparedJob:
    index: int
    prompt: str
    model: str
    path: Path
    content: list
    account_ids: tuple = ()
    account_limits: dict = None


class GenerationFailure(RuntimeError):
    def __init__(self, message, *, status="failed", submission_attempted=True,
                 account_rejected=False):
        super().__init__(message)
        self.status = status
        self.submission_attempted = submission_attempted
        self.account_rejected = account_rejected


def is_explicit_account_rejection(message):
    """Recognize backend-owned identity errors, not arbitrary mentions of 401 or captcha."""
    if not isinstance(message, str):
        return False
    text = message.strip()
    return bool(re.match(
        r"^(?:指定账号\s*#\d+\s*生成失败[:：]\s*)?Flow 登录认证失败(?:[:：]|$)", text
    ) or re.fullmatch(
        r"指定账号\s*#\d+\s*当前不可用于该模型[，,]\s*或登录验证失败", text
    ))


def settings():
    value = json.loads(CONFIG_PATH.read_text())
    if not value.get("api_key"):
        raise ValueError("Flow2API API key is missing from the private settings file.")
    value["base_url"] = value["base_url"].rstrip("/")
    job_timeout_seconds(value)
    return value


def job_timeout_seconds(cfg):
    value = cfg.get("job_timeout_seconds", DEFAULT_JOB_TIMEOUT_SECONDS)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= MAX_JOB_TIMEOUT_SECONDS or not math.isfinite(value)):
        raise ValueError("job_timeout_seconds must be a finite number greater than 0 and at most 3600.")
    return float(value)


def submission_not_sent(error):
    """Only a backend-owned structured marker establishes that generation was not sent."""
    return (isinstance(error, dict)
            and not any(error.get(flag) for flag in ("submission_uncertain", "outcome_unknown"))
            and (error.get("submission_not_sent") is True
                 or error.get("generation_submitted") is False))


def api_client(cfg):
    timeout = job_timeout_seconds(cfg)
    return httpx.AsyncClient(
        base_url=cfg["base_url"] + "/",
        headers={"Authorization": "Bearer " + cfg["api_key"]},
        timeout=httpx.Timeout(timeout, connect=30.0, read=timeout, write=timeout),
        trust_env=False,
    )


async def models(cfg):
    async with api_client(cfg) as client:
        response = await client.get("models")
        response.raise_for_status()
        return [item for item in response.json()["data"]
                if item["id"].startswith("gemini-") and "image" in item["id"]]


async def check_connection():
    cfg = settings()
    catalog = await models(cfg)
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        response = await client.get(cfg["base_url"].removesuffix("/v1") + "/health")
        response.raise_for_status()
        health = response.json()
    return {"connected": True, "base_url": cfg["base_url"],
            "active_accounts": health.get("active_tokens", 0),
            "image_model_count": len(catalog), "default_model": DEFAULT_MODEL}


def positive_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(label + " must be a positive integer.")
    return value


async def image_accounts(cfg, model=DEFAULT_MODEL):
    async with api_client(cfg) as client:
        response = await client.get("image-accounts", params={"model": model})
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("accounts"), list):
        raise ValueError("Flow2API did not return a valid image account list.")
    for account in result["accounts"]:
        if not isinstance(account, dict):
            raise ValueError("Flow2API returned an invalid image account.")
        positive_integer(account.get("id"), "Account ID")
        if "concurrency_limit" in account:
            positive_integer(account["concurrency_limit"], "Account concurrency_limit")
    return result


def output_file(output_path, cfg):
    if output_path is not None:
        if not isinstance(output_path, str) or not output_path:
            raise ValueError("output_path must be an absolute .png file path.")
        path = Path(output_path).expanduser()
    else:
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex + ".png"
        path = Path(cfg["output_directory"]).expanduser() / name
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise ValueError("output_path must be an absolute .png file path.")
    # Resolve parent aliases before comparing paths or acquiring output reservations.
    path = path.resolve()
    if path.exists():
        raise ValueError("Output already exists; choose a new file name: " + str(path))
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError("Output parent is not a directory: " + str(ancestor))
    return path


async def prepare_jobs(jobs, cfg, *, require_accounts):
    job_timeout_seconds(cfg)
    allowed = {item["id"] for item in await models(cfg)}
    prepared, outputs = [], set()
    permitted = {"prompt", "model", "output_path", "reference_image_paths"}
    for index, job in enumerate(jobs):
        if not isinstance(job, dict) or set(job) - permitted:
            raise ValueError(f"Job {index}: unsupported job fields.")
        prompt = job.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise ValueError(f"Job {index}: a nonempty prompt of at most 20000 characters is required.")
        model = job.get("model", DEFAULT_MODEL)
        if not isinstance(model, str) or model not in allowed:
            raise ValueError(f"Job {index}: unknown image model; call flow2api_list_image_models first.")
        refs = job.get("reference_image_paths")
        if refs is None:
            refs = []
        if not isinstance(refs, list) or len(refs) > 4 or any(not isinstance(ref, str) for ref in refs):
            raise ValueError(f"Job {index}: at most four reference image file paths are supported.")
        path = output_file(job.get("output_path"), cfg)
        if path in outputs:
            raise ValueError(f"Job {index}: duplicate output path: {path}")
        outputs.add(path)
        content = [{"type": "text", "text": prompt.strip()}]
        content.extend(reference_part(filename) for filename in refs)
        prepared.append(PreparedJob(index, prompt.strip(), model, path, content))
    if require_accounts:
        by_model = {}
        for model in dict.fromkeys(job.model for job in prepared):
            result = await image_accounts(cfg, model)
            account_ids = tuple(dict.fromkeys(account["id"] for account in result["accounts"]))
            if not account_ids:
                raise ValueError("No configured eligible image account for model: " + model)
            by_model[model] = (account_ids, {account["id"]: min(
                account.get("concurrency_limit", MAX_ACCOUNT_CONCURRENCY), MAX_ACCOUNT_CONCURRENCY)
                for account in result["accounts"]})
        for job in prepared:
            job.account_ids, job.account_limits = by_model[job.model]
    return prepared


class OutputReservations:
    """Prevent calls using this integration from generating into the same filename."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.reservations = []

    def __enter__(self):
        try:
            for job in self.jobs:
                job.path.parent.mkdir(parents=True, exist_ok=True)
                reservation = job.path.with_name("." + job.path.name + ".flow2api-reserved")
                try:
                    descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    raise ValueError("Output is reserved by another generation: " + str(job.path)) from None
                os.close(descriptor)
                self.reservations.append(reservation)
                if job.path.exists():
                    raise ValueError("Output already exists; choose a new file name: " + str(job.path))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        for reservation in self.reservations:
            reservation.unlink(missing_ok=True)
        self.reservations.clear()


def write_batch_manifest(path, result):
    """Atomically retain outcomes without prompts, reference bytes or API configuration."""
    snapshot = {**result, "jobs": []}
    for job in result["jobs"]:
        item = dict(job)
        if str(item.get("image_url", "")).startswith("data:"):
            item.pop("image_url")
            item["inline_image_url_omitted"] = True
        snapshot["jobs"].append(item)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(snapshot, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reference_part(filename):
    path = Path(filename).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Reference image must be an existing absolute file path.")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Reference image exceeds 25 MiB.")
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as image:
        mime = Image.MIME.get(image.format)
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Reference images must be PNG, JPEG or WebP.")
        image.verify()
    return {"type": "image_url", "image_url": {
        "url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")}}


async def read_image(url, cfg):
    if url.startswith("data:image/"):
        header, encoded = url.split(",", 1)
        if ";base64" not in header or len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 16:
            raise ValueError("Invalid or oversized generated image.")
        return base64.b64decode(encoded, validate=True)
    parsed = urlparse(url)
    api_host = urlparse(cfg["base_url"]).hostname
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("The API did not return a valid image URL.")
    if parsed.scheme == "http" and parsed.hostname != api_host:
        raise ValueError("Unencrypted generated image URL outside the local API.")
    # No API credentials are forwarded to media hosts.
    is_local = parsed.hostname in {"127.0.0.1", "localhost", api_host}
    proxy = None if is_local else (cfg.get("media_proxy") or os.environ.get("https_proxy") or "http://127.0.0.1:17897")
    # Media GETs are safe to retry; this loop must never wrap generation POSTs.
    deadline = asyncio.get_running_loop().time() + min(job_timeout_seconds(cfg), MEDIA_GET_TIMEOUT_SECONDS)
    async with asyncio.timeout_at(deadline):
        for attempt in range(MEDIA_GET_ATTEMPTS):
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(remaining, connect=min(15.0, remaining)),
                                             follow_redirects=True, proxy=proxy, verify=True, trust_env=False) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_IMAGE_BYTES:
                                raise ValueError("Generated image exceeds 25 MiB.")
                            chunks.append(chunk)
                        return b"".join(chunks)
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if attempt + 1 >= MEDIA_GET_ATTEMPTS:
                    raise
                delay = 0.5 * (attempt + 1)
                if isinstance(error, httpx.HTTPStatusError):
                    status = error.response.status_code
                    if status not in {408, 429} and status < 500:
                        raise
                    if status == 429 and "Retry-After" in error.response.headers:
                        try:
                            delay = float(error.response.headers["Retry-After"])
                        except ValueError:
                            # An unsupported/long server cooldown must not become a retry storm.
                            raise error from None
                        if not math.isfinite(delay) or not 0 <= delay <= 2.0:
                            raise
                if delay >= deadline - asyncio.get_running_loop().time():
                    raise
                await asyncio.sleep(delay)


async def submit_job(job, cfg, account_id=None, *, deadline=None):
    payload = {"model": job.model, "stream": True,
               "messages": [{"role": "user", "content": job.content}]}
    if account_id is not None:
        payload["account_id"] = account_id
    pieces, done, generated_assets = [], False, {}
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + job_timeout_seconds(cfg)
    try:
        async with asyncio.timeout_at(deadline), api_client(cfg) as client:
            async with client.stream("POST", "chat/completions", json=payload) as response:
                if response.is_error:
                    # Preserve trusted pre-submission markers in non-stream HTTP errors too.
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        continue
                    if not data:
                        continue
                    event = json.loads(data)
                    assets = event.get("generated_assets") or (event.get("metadata") or {}).get("generated_assets")
                    if isinstance(assets, dict):
                        generated_assets.update(assets)
                    if event.get("error"):
                        error = event["error"]
                        uncertain = isinstance(error, dict) and any(error.get(flag) for flag in (
                            "submission_uncertain", "outcome_unknown"))
                        message = error.get("message", "Flow2API generation failed") if isinstance(error, dict) else str(error)
                        raise GenerationFailure(message, status="unknown" if uncertain else "failed",
                                                submission_attempted=not submission_not_sent(error),
                                                account_rejected=is_explicit_account_rejection(message))
                    for choice in event.get("choices", []):
                        text = choice.get("delta", {}).get("content", "")
                        if isinstance(text, str):
                            pieces.append(text)
    except GenerationFailure as error:
        urls = IMAGE_RE.findall("".join(pieces))
        if urls:
            return urls[-1], "The API reported an error after returning an image URL.", generated_assets, {
                "message": str(error), "status": error.status,
                "account_rejected": error.account_rejected}
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        raise GenerationFailure(str(error), submission_attempted=False) from error
    except httpx.HTTPStatusError as error:
        try:
            body = error.response.json()
            backend_error = body.get("error") if isinstance(body, dict) else None
        except (ValueError, httpx.ResponseNotRead):
            backend_error = None
        if submission_not_sent(backend_error):
            raise GenerationFailure(backend_error.get("message") or str(error),
                                    submission_attempted=False,
                                    account_rejected=error.response.status_code == 401) from error
        uncertain = isinstance(backend_error, dict) and any(backend_error.get(flag) for flag in (
            "submission_uncertain", "outcome_unknown"))
        status = "unknown" if uncertain or error.response.status_code >= 500 or error.response.status_code == 408 else "failed"
        raise GenerationFailure(str(error), status=status,
                                account_rejected=error.response.status_code == 401) from error
    except Exception as error:
        # A timeout/reset/invalid stream after POST may have reached Google. Never replay it.
        urls = IMAGE_RE.findall("".join(pieces))
        if urls:
            return urls[-1], f"{type(error).__name__}: {error}", generated_assets, {
                "message": "The stream failed after returning an image URL.", "status": "unknown"}
        raise GenerationFailure(f"{type(error).__name__}: {error}", status="unknown") from error
    urls = IMAGE_RE.findall("".join(pieces))
    if not urls:
        message = "The API completed without returning an image; check Flow2API request logs."
        raise GenerationFailure(message, status="failed" if done else "unknown")
    warning = None if done else "Image URL recovered from a stream without a completion marker."
    return (urls[-1], warning, generated_assets,
            None if done else {"message": warning, "status": "unknown"})


async def save_image(url, path, cfg):
    raw = await read_image(url, cfg)
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
    # Exclusive creation prevents concurrent calls overwriting an existing asset.
    created = False
    try:
        with path.open("xb") as target:
            created = True
            image.save(target, format="PNG")
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    preview = image.copy()
    preview.thumbnail((1024, 1024))
    buffer = io.BytesIO()
    preview.save(buffer, format="PNG")
    return {"success": True, "path": str(path),
            "width": image.width, "height": image.height}, buffer.getvalue()


async def execute_job(job, cfg, account_id=None):
    # The deadline includes both generation and retrieving the delivered image.
    deadline = asyncio.get_running_loop().time() + job_timeout_seconds(cfg)
    result = {"job_index": job.index, "status": "uncompleted", "model": job.model,
              "output_path": str(job.path), "submission_attempted": False}
    if account_id is not None:
        result["account_id"] = account_id
    try:
        url, warning, assets, post_image_error = await submit_job(job, cfg, account_id, deadline=deadline)
        result.update(submission_attempted=True, generation_completed=True, image_url=url)
        if assets:
            result["generated_assets"] = assets
        upscale = assets.get("upscaled_image")
        if isinstance(upscale, dict) and any(upscale.get(flag) for flag in ("submission_uncertain", "outcome_unknown")):
            result["upscale_status"] = "unknown"
            result["outcome_unknown"] = True
            result["warnings"] = ["Upscaling outcome is unknown; the original image is retained. Do not automatically resubmit this job or upscale it again."]
        if warning:
            result["stream_warning"] = warning
        if post_image_error:
            result["post_image_error"] = post_image_error
            if post_image_error.get("account_rejected"):
                result["account_rejected"] = True
            result.setdefault("warnings", []).append(
                "An image URL was retained after a stream error; do not automatically resubmit this job.")
            if post_image_error["status"] == "unknown":
                result["outcome_unknown"] = True
    except GenerationFailure as error:
        result.update(status=error.status, error=str(error),
                      submission_attempted=error.submission_attempted)
        if error.account_rejected:
            result["account_rejected"] = True
        if not error.submission_attempted:
            result["submission_not_sent"] = True
        return result, None
    try:
        async with asyncio.timeout_at(deadline):
            saved, preview = await save_image(url, job.path, cfg)
        result.update(saved, status="succeeded")
        if post_image_error and post_image_error["status"] == "failed":
            result.update(status="failed", success=False, error=post_image_error["message"])
        return result, preview
    except Exception as error:
        # Generation is known to have completed; preserve its URL for retrieval without another POST.
        # HTTPX exception text can contain a signed media URL. Retain that URL only
        # in the existing private result/manifest, not in a diagnostic message.
        result.update(status="failed", delivery_recoverable=True,
                      error=f"Image returned but could not be saved: {type(error).__name__}")
        return result, None


async def recover_image(batch_id, job_index, manifest_directory):
    """Retrieve one known generated image. This path has no generation API call."""
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise ValueError("batch_id must be an existing 32-character batch identifier.")
    if isinstance(job_index, bool) or not isinstance(job_index, int) or job_index < 0:
        raise ValueError("job_index must be a nonnegative integer.")
    if not isinstance(manifest_directory, str) or not Path(manifest_directory).is_absolute():
        raise ValueError("manifest_directory must be the existing absolute batch directory.")
    directory = Path(manifest_directory).resolve(strict=True)
    manifest_path = directory / (".flow2api-batch-" + batch_id + ".json")
    if manifest_path.is_symlink():
        raise ValueError("Batch manifest must not be a symbolic link.")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("batch_id") != batch_id
            or manifest.get("batch_state") not in {"completed", "paused", "cancelled", "interrupted"}
            or not isinstance(manifest.get("jobs"), list)
            or any(row.get("status") == "running" for row in manifest["jobs"])):
        raise ValueError("Recovery requires the matching finished batch; active batches cannot be changed.")
    matches = [row for row in manifest["jobs"] if row.get("job_index") == job_index]
    if len(matches) != 1:
        raise ValueError("Batch does not contain exactly one matching job.")
    job = matches[0]
    if (job.get("generation_completed") is not True or job.get("outcome_unknown")
            or job.get("status") not in {"failed", "succeeded"}
            or not isinstance(job.get("image_url"), str)
            or job["image_url"].startswith("data:")):
        raise ValueError("Only a known generated media URL can be recovered; never resubmit an unknown job.")
    output = job.get("output_path")
    if not isinstance(output, str) or not Path(output).is_absolute() or Path(output).suffix.lower() != ".png":
        raise ValueError("Stored job does not have an absolute PNG output path.")
    path = Path(output)
    if path != path.resolve():
        raise ValueError("Stored output path must be canonical, without symbolic-link aliases.")
    fingerprint = hashlib.sha256(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    receipt = directory / f".flow2api-recovery-{batch_id}-{job_index}.json"
    # A successful repeat verifies the original operation's file; it cannot GET or POST again.
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if (saved.get("source_job_sha256") != fingerprint or saved.get("path") != str(path)
                or not path.is_file() or saved.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError("Recovery record or saved image changed; reconcile it before continuing.")
        with Image.open(path) as image:
            image.load()
            image.thumbnail((1024, 1024))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        return {**saved, "recovered_from_cache": True}, buffer.getvalue()
    cfg = settings()
    prepared = PreparedJob(job_index, "", job.get("model", DEFAULT_MODEL), path, [])
    with OutputReservations([prepared]):
        try:
            saved, preview = await save_image(job["image_url"], path, cfg)
        except Exception as error:
            # Do not stringify exceptions that may embed a signed URL.
            raise RuntimeError("Image delivery recovery failed: " + type(error).__name__) from None
        saved.update(status="succeeded", batch_id=batch_id, job_index=job_index,
                     submission_attempted=False, generation_replayed=False, delivery_only=True,
                     original_job_status=job["status"], account_id=job.get("account_id"),
                     source_manifest_path=str(manifest_path), source_job_sha256=fingerprint,
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                     recovery_manifest_path=str(receipt))
        write_batch_manifest(receipt, {**saved, "jobs": []})
    return saved, preview


async def generate_image(prompt, model=DEFAULT_MODEL, output_path=None,
                         reference_image_paths=None, account_id=None):
    cfg = settings()
    jobs = await prepare_jobs([{"prompt": prompt, "model": model, "output_path": output_path,
                                "reference_image_paths": reference_image_paths}], cfg,
                               require_accounts=account_id is not None)
    if account_id is not None:
        positive_integer(account_id, "account_id")
        if account_id not in jobs[0].account_ids:
            raise ValueError("The selected account is not configured for this image model.")
    with OutputReservations(jobs):
        result, preview = await execute_job(jobs[0], cfg, account_id)
    if result["status"] != "succeeded":
        raise GenerationFailure(json.dumps(result, ensure_ascii=False), status=result["status"],
                                submission_attempted=result["submission_attempted"])
    # Keep the existing single-image success contract, with optional account metadata.
    return result, preview


async def generate_images(jobs, concurrency=None):
    cfg = settings()
    max_jobs = min(positive_integer(cfg.get("batch_max_jobs", MAX_BATCH_JOBS), "batch_max_jobs"), MAX_BATCH_JOBS)
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= max_jobs:
        raise ValueError(f"jobs must contain between 1 and {max_jobs} independent image jobs.")
    prepared = await prepare_jobs(jobs, cfg, require_accounts=True)
    account_limits = {}
    for job in prepared:
        for account_id, capacity in job.account_limits.items():
            account_limits[account_id] = min(account_limits.get(account_id, capacity), capacity)
    max_concurrency = sum(account_limits.values())
    if "batch_max_concurrency" in cfg:
        max_concurrency = min(max_concurrency, positive_integer(cfg["batch_max_concurrency"], "batch_max_concurrency"))
    if concurrency is None:
        concurrency = min(positive_integer(cfg.get("batch_default_concurrency", max_concurrency),
                                           "batch_default_concurrency"), max_concurrency)
    positive_integer(concurrency, "concurrency")
    if concurrency > max_concurrency:
        raise ValueError(f"concurrency must not exceed configured eligible account capacity ({max_concurrency}).")
    limit = min(concurrency, len(prepared))
    results = [{"job_index": job.index, "status": "uncompleted", "model": job.model,
                "output_path": str(job.path), "submission_attempted": False} for job in prepared]
    previews, pending, running, busy, uses = {}, list(prepared), {}, {}, {}
    batch_id = uuid.uuid4().hex
    manifest_path = prepared[0].path.parent / (".flow2api-batch-" + batch_id + ".json")
    batch_state = "prepared"
    pause_reason = None

    def snapshot():
        counts = {status: sum(result["status"] == status for result in results)
                  for status in ("succeeded", "failed", "unknown", "uncompleted")}
        success = counts["succeeded"] == len(prepared) and not any(job.get("outcome_unknown") for job in results)
        result = {"success": success, "partial": counts["succeeded"] > 0 and not success,
                "batch_id": batch_id, "batch_state": batch_state, "manifest_path": str(manifest_path),
                "requested_count": len(prepared), "concurrency": limit, "counts": counts,
                "per_account_limits": [{"account_id": account, "concurrency_limit": capacity}
                                       for account, capacity in sorted(account_limits.items())],
                "account_availability": "configured; runtime authentication and quota can change",
                "jobs": results}
        if pause_reason is not None:
            result["pause_reason"] = pause_reason
        return result

    stopped = False
    with OutputReservations(prepared):
        try:
            write_batch_manifest(manifest_path, snapshot())
            batch_state = "running"
            while pending or running:
                while pending and len(running) < limit and not stopped:
                    selected = next(((job, min((account for account in job.account_ids
                                               if busy.get(account, 0) < account_limits[account]),
                                              key=lambda account: (busy.get(account, 0) / account_limits[account],
                                                                   uses.get(account, 0) / account_limits[account], account)))
                                     for job in pending if any(busy.get(account, 0) < account_limits[account]
                                                             for account in job.account_ids)), None)
                    if selected is None:
                        break
                    job, account_id = selected
                    pending.remove(job)
                    busy[account_id] = busy.get(account_id, 0) + 1
                    uses[account_id] = uses.get(account_id, 0) + 1
                    task = asyncio.create_task(execute_job(job, cfg, account_id))
                    running[task] = (job, account_id)
                    results[job.index].update(status="running", account_id=account_id,
                                              submission_attempted=True)
                write_batch_manifest(manifest_path, snapshot())
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                # Drain observed completions before launching more jobs. Rejections never trigger failover.
                for task in sorted(done, key=lambda item: running[item][0].index):
                    job, account_id = running[task]
                    result, preview = task.result()
                    running.pop(task)
                    busy[account_id] -= 1
                    results[job.index] = result
                    if preview is not None:
                        previews[job.index] = preview
                    if result.get("account_rejected"):
                        stopped = True
                        if pause_reason is None or pause_reason["code"] == "outcome_unknown":
                            pause_reason = {"code": "account_rejected", "job_index": job.index,
                                            "account_id": account_id,
                                            "message": "Assigned account authentication or model eligibility was explicitly rejected. Repair and revalidate it before starting a new batch; queued jobs were not submitted.",
                                            "error": result.get("error") or result.get("post_image_error", {}).get("message")}
                    elif result["status"] == "unknown" or result.get("outcome_unknown"):
                        stopped = True
                        if pause_reason is None:
                            pause_reason = {"code": "outcome_unknown", "job_index": job.index,
                                            "account_id": account_id,
                                            "message": "A submission outcome is unknown; inspect request logs before any retry."}
                write_batch_manifest(manifest_path, snapshot())
            for job in pending:
                if pause_reason and pause_reason["code"] == "account_rejected":
                    results[job.index]["error"] = "Not submitted: batch paused after explicit account authentication or model eligibility rejection; repair and revalidate the account before a new batch."
                else:
                    results[job.index]["error"] = "Not submitted: batch paused after an unknown submission; inspect request logs before any retry."
            batch_state = "paused" if stopped else "completed"
            write_batch_manifest(manifest_path, snapshot())
        except BaseException as error:
            tasks = list(running)
            for task in tasks:
                if not task.done():
                    task.cancel()
            finished = await asyncio.gather(*tasks, return_exceptions=True)
            for task, completion in zip(tasks, finished):
                job, account_id = running[task]
                if isinstance(completion, tuple):
                    result, preview = completion
                    results[job.index] = result
                    if preview is not None:
                        previews[job.index] = preview
                else:
                    results[job.index].update(status="unknown", account_id=account_id,
                                              submission_attempted=True,
                                              error="Batch interrupted while job was running; inspect request logs before any retry.")
            running.clear()
            for job in pending:
                results[job.index]["error"] = "Not submitted: batch interrupted."
            batch_state = "cancelled" if isinstance(error, asyncio.CancelledError) else "interrupted"
            write_batch_manifest(manifest_path, snapshot())
            raise
        finally:
            # A cancelled caller must not leave workers using filenames after their reservations are released.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
    return snapshot(), previews


@app.list_tools()
async def list_tools():
    job_properties = {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
        "model": {"type": "string", "default": DEFAULT_MODEL},
        "output_path": {"type": "string", "description": "Fresh absolute PNG file path in the current project; omit for the configured output folder."},
        "reference_image_paths": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
    }
    return [
        types.Tool(name="flow2api_check_connection",
                   description="Check local Flow2API connection, API key and active account count without generating an image.",
                   inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        types.Tool(name="flow2api_list_image_models",
                   description="List available Google Flow image model IDs and descriptions. Does not generate images.",
                   inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        types.Tool(name="flow2api_list_image_accounts",
                   description="List configured eligible account IDs and concurrency limits for an image model without generating an image. This is configuration eligibility; authentication and quota may change at runtime.",
                   inputSchema={"type": "object", "properties": {
                       "model": {"type": "string", "default": DEFAULT_MODEL}}, "additionalProperties": False}),
        types.Tool(name="flow2api_recover_image",
                   description="Download one image already generated by a finished batch, using its recorded URL and original output path. No generation request, account change or Flow credits. Never use for an unknown submission. Does not overwrite files; repeated recovery verifies the saved PNG.",
                   inputSchema={"type": "object", "properties": {
                       "batch_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                       "job_index": {"type": "integer", "minimum": 0},
                       "manifest_directory": {"type": "string", "description": "Existing absolute directory containing the private .flow2api-batch-ID.json manifest."}},
                       "required": ["batch_id", "job_index", "manifest_directory"], "additionalProperties": False}),
        types.Tool(name="flow2api_generate_image",
                   description="Generate one image through the user's local Google Flow account, save a PNG and return its actual image preview. Can consume Flow credits. Supports text-to-image and optional local reference images. Use an absolute output_path in the current project; choose a fresh name. Defaults to Nano Banana 2.1 landscape.",
                   inputSchema={"type": "object", "properties": {
                       **job_properties, "account_id": {"type": "integer", "minimum": 1,
                       "description": "Optional strict account pin. Omit to retain normal load balancing."}},
                                "required": ["prompt"], "additionalProperties": False}),
        types.Tool(name="flow2api_generate_images",
                   description="Generate several independent images, one per job. Preflight all models, output paths and reference images before submitting any job. Pin jobs to eligible accounts, balance their load and respect each account's concurrency limit (at most 8); queue excess jobs. Preserve partial successes and return each job's actual preview. Unknown submissions or explicit account authentication/eligibility rejections pause jobs not yet started; never automatically replay or switch accounts for a failed job. May consume Flow credits for each submitted job.",
                   inputSchema={"type": "object", "properties": {
                       "jobs": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH_JOBS,
                                "items": {"type": "object", "properties": job_properties,
                                          "required": ["prompt"], "additionalProperties": False}},
                       "concurrency": {"type": "integer", "minimum": 1,
                                       "description": "Optional tighter global concurrency cap; defaults to total eligible account capacity and cannot exceed it."}},
                                "required": ["jobs"], "additionalProperties": False}),
    ]


@app.call_tool()
async def call_tool(name, arguments):
    try:
        if name == "flow2api_check_connection":
            result = await check_connection()
        elif name == "flow2api_list_image_models":
            result = await models(settings())
        elif name == "flow2api_list_image_accounts":
            result = await image_accounts(settings(), **(arguments or {}))
        elif name == "flow2api_generate_image":
            result, preview = await generate_image(**arguments)
            return types.CallToolResult(content=[
                types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                types.ImageContent(type="image", data=base64.b64encode(preview).decode("ascii"), mimeType="image/png")])
        elif name == "flow2api_recover_image":
            result, preview = await recover_image(**arguments)
            return types.CallToolResult(content=[
                types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                types.ImageContent(type="image", data=base64.b64encode(preview).decode("ascii"), mimeType="image/png")])
        elif name == "flow2api_generate_images":
            result, previews = await generate_images(**arguments)
            content = [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            for job in result["jobs"]:
                if job["job_index"] in previews:
                    content.append(types.TextContent(type="text", text=json.dumps({
                        "preview_for_job_index": job["job_index"], "account_id": job["account_id"],
                        "path": job["path"]}, ensure_ascii=False)))
                    content.append(types.ImageContent(type="image", data=base64.b64encode(
                        previews[job["job_index"]]).decode("ascii"), mimeType="image/png"))
            return types.CallToolResult(isError=not result["success"], content=content)
        else:
            raise ValueError("Unknown tool: " + name)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])
    except Exception as error:
        # No automatic resubmission of image generation requests.
        return types.CallToolResult(isError=True, content=[types.TextContent(
            type="text", text=f"{type(error).__name__}: {error}")])


async def run_generation_cli(operation):
    """Let generation cleanup persist uncertain outcomes before a SIGTERM exit."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    termination_requested = False
    active = True
    previous_handler = signal.getsignal(signal.SIGTERM)

    def cancel_generation():
        if active:
            task.cancel()

    def request_termination(_signum, _frame):
        nonlocal termination_requested
        if not termination_requested:
            termination_requested = True
            loop.call_soon_threadsafe(cancel_generation)

    try:
        signal.signal(signal.SIGTERM, request_termination)
        result = await operation()
        if termination_requested:
            raise SystemExit(128 + signal.SIGTERM)
        return result
    except asyncio.CancelledError:
        if termination_requested:
            raise SystemExit(128 + signal.SIGTERM) from None
        raise
    finally:
        active = False
        signal.signal(signal.SIGTERM, previous_handler)


async def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--list-accounts", action="store_true", help="List configured eligible image account IDs/capacities for --model without generating.")
    action.add_argument("--generate")
    action.add_argument("--generate-batch", type=Path, help="JSON file containing an array of independent image jobs.")
    action.add_argument("--recover-batch", help="Existing batch ID to retrieve from its private manifest, without generation.")
    parser.add_argument("--job-index", type=int)
    parser.add_argument("--manifest-directory")
    parser.add_argument("--output")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--concurrency", type=int)
    args = parser.parse_args()
    CONFIG_PATH = args.config
    if args.recover_batch is not None:
        if args.job_index is None or args.manifest_directory is None:
            parser.error("--recover-batch requires --job-index and --manifest-directory")
        if args.output or args.reference or args.account_id is not None or args.concurrency is not None:
            parser.error("recovery uses only the recorded output path and account; generation options are not accepted")
    elif args.job_index is not None or args.manifest_directory is not None:
        parser.error("--job-index and --manifest-directory require --recover-batch")
    if args.concurrency is not None and args.generate_batch is None:
        parser.error("--concurrency requires --generate-batch")
    if args.generate_batch is not None and (args.output or args.reference or args.account_id is not None):
        parser.error("--output, --reference and --account-id apply only to --generate; put batch paths/references in each job")
    if args.check:
        print(json.dumps(await check_connection(), ensure_ascii=False))
    elif args.list_accounts:
        print(json.dumps(await image_accounts(settings(), args.model), ensure_ascii=False))
    elif args.recover_batch is not None:
        result, _ = await run_generation_cli(lambda: recover_image(
            args.recover_batch, args.job_index, args.manifest_directory))
        print(json.dumps(result, ensure_ascii=False))
    elif args.generate:
        result, _ = await run_generation_cli(lambda: generate_image(
            args.generate, model=args.model, output_path=args.output,
            reference_image_paths=args.reference, account_id=args.account_id))
        print(json.dumps(result, ensure_ascii=False))
    elif args.generate_batch is not None:
        jobs = json.loads(args.generate_batch.read_text())
        result, _ = await run_generation_cli(lambda: generate_images(jobs, concurrency=args.concurrency))
        print(json.dumps(result, ensure_ascii=False))
        if not result["success"]:
            raise SystemExit(1)
    else:
        async with stdio_server() as (reader, writer):
            await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
