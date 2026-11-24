# Flow2API image MCP

The existing `flow2api_generate_image` tool still generates one image. Its optional
`account_id` strictly selects one configured eligible account. Omitting that field
keeps the backend's normal account selection.

`flow2api_generate_images` accepts independent jobs:

```json
{
  "jobs": [
    {"prompt": "An orange cat", "output_path": "/project/assets/cat.png"},
    {"prompt": "A blue bird", "model": "gemini-3.1-flash-image-landscape",
     "output_path": "/project/assets/bird.png",
     "reference_image_paths": ["/project/assets/reference.png"]}
  ],
  "concurrency": 2
}
```

Each job produces one image through one POST request. A batch has at most 32 jobs.
Every model, reference image, output filename and model's eligible account pool is
checked before any generation is submitted. Outputs must be fresh absolute PNG
paths. Canonical path aliases cannot point two jobs at the same output. Sibling
reservation files prevent concurrent MCP calls from using the same filename.

The free `flow2api_list_image_accounts` tool accepts an optional `model` and lists
configured account IDs and capacities. Eligibility does not guarantee current
authentication or quota. A batch pins each job to a compatible account and balances
running load across those accounts. It respects the returned `concurrency_limit`,
with a maximum of 8 per account. When the field is absent, the limit is 8. Default
global concurrency is the sum of those capacities, bounded by the job count.
Two eligible accounts with capacity 8 allow up to 16 concurrent jobs.
Optional `concurrency` only tightens that limit. Excess jobs wait. The backend also
enforces its account limit across separate clients and batches.

Results remain in input order and contain `job_index`, `account_id`, `status` and
the actual saved path/dimensions for delivered images. Each preview is preceded by
a text item identifying its job and file. Statuses are:

| Status | Meaning |
| --- | --- |
| `succeeded` | An image was saved and a preview is available. Check warnings for a failed or uncertain later stage. |
| `failed` | A known rejection or failure occurred. A returned image URL/file is retained if available. |
| `unknown` | Submission may have reached the upstream service, but its outcome is unknown. |
| `uncompleted` | The job was not submitted because the queue paused after an unknown outcome. |

An unknown outcome pauses jobs that have not started. Already running jobs finish
and their results are preserved. There are no automatic POST retries, account
failovers in pinned jobs, or replay of unknown jobs. Inspect request logs before
manually deciding whether to submit another job. If an image URL arrived before a
stream error, the integration attempts to save that image without another POST.
An uncertain upscale keeps the original image and its marker/warning; it pauses
the queue and never automatically repeats the upscale or original job.

The private `settings.json` supports optional positive integer limits:
`batch_max_jobs` (default and hard maximum 32), `batch_max_concurrency` (a tighter
global cap), and `batch_default_concurrency` (a tighter default). API credentials
remain in the existing private settings file.

Generated media downloads use at most three GET attempts within 60 seconds and
the remaining generation deadline. TLS verification stays enabled. HTTP 401/403
are not retried; 429 is retried only for a numeric `Retry-After` of at most two
seconds. Generation POSTs are never retried by this download logic.

For an image that was generated but could not be saved, use the finished batch's
recorded identity, without a new prompt, URL, account or output filename:

```bash
.venv/bin/python server.py --recover-batch BATCH_ID --job-index 0 --manifest-directory /project/assets
```

The equivalent `flow2api_recover_image` tool accepts `batch_id`, `job_index` and
`manifest_directory`. It refuses active batches, unknown jobs and existing output
files. It only downloads the URL already recorded for that job, and creates the
original output exclusively. A private recovery receipt records the source job
hash and resulting PNG hash. Repeating the same recovery verifies that PNG without
another download or generation. The original batch manifest remains unchanged so
an adapter can reconcile its own staged output and promotion separately. This
does not introduce a global output lock: later intentionally requested generations
still follow the existing output-path and adapter rules.

## CLI

```bash
.venv/bin/python server.py --list-accounts --model gemini-3.1-flash-image-landscape
.venv/bin/python server.py --generate-batch /project/jobs.json --concurrency 2
.venv/bin/python server.py --generate "An orange cat" --output /project/cat.png --account-id 1
```

`jobs.json` contains the job array from the MCP example, without its outer object.
`--config /absolute/settings.json` can select another configuration. Batch CLI
prints the full result and exits 1 for a partial/unknown result, preserving
successful files. The existing single-image CLI and `--check` remain available.

Each batch also returns `manifest_path`, a private `.flow2api-batch-ID.json` file
in the first job's output directory. It is updated atomically as jobs finish.
The file contains statuses, account assignments and output metadata, with mode
0600; it omits prompts, reference bytes, API configuration and inline image data.
If the caller cancels the batch, completed results stay recorded, running jobs
become `unknown`, and jobs that never started stay `uncompleted`. Cancellation
still propagates to the caller. Inspect this manifest and backend request logs
before deciding how to continue; successful jobs do not need another submission.

## Offline verification

```bash
.venv/bin/python -m unittest test_server -q
```

The suite uses synthetic in-memory images, a mock HTTP transport, and a loopback
HTTP server for actual MCP stdio and CLI checks. It does not contact Google or
consume Flow credits.
