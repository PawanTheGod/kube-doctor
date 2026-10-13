# Kube Snapshot Doctor

![CI](https://github.com/AdiB1619/kube-snapshot-doctor/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)
![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![evals](https://img.shields.io/badge/evals-8%20cases-blue.svg)

> Forkers: replace `AdiB1619` with your GitHub organisation in the badge links and
> in `pyproject.toml` before publishing.

Give it a screenshot of a failing Kubernetes workload — a `kubectl get pods`
table, a `describe` dump, a Grafana panel, k9s, Lens, a cloud console — plus
optional logs. It returns a ranked diagnosis where every cause cites something
visible in the input, followed by read-only next steps.

It never connects to a cluster and never runs a command. It suggests; you decide.

Default backend is an open-weight **Gemma** model served through the Gemini API.
Any OpenAI-compatible vision endpoint (Ollama, vLLM, llama.cpp, LM Studio) works
as an alternative.

```
screenshot + logs
   -> redact secrets from text
   -> Stage 1  OBSERVE   (vision)  what is literally visible      -> observations
   -> Stage 2  DIAGNOSE  (text)    what it means, given a failure
                                   mode reference and the logs    -> diagnosis
   -> validate (pydantic) -> one repair retry on failure
   -> safety lint on every suggested command
   -> render json | markdown
```

Two stages because separating *what I see* from *what it means* is where vision
models hallucinate. Stage 1 sees pixels and is told to transcribe; Stage 2 never
sees pixels at all, only the transcription, and is told to reason only from it.
That also makes each prompt independently testable, and lets `--verbose` log the
observations so a bad diagnosis can be traced to a bad transcription.

## Verification status

The pipeline, the CLI, and the eval harness are tested. The model side is
partly exercised: a single live call has been made, and CI has no credentials,
so the model is not *benchmarked*. Read the table before you quote any number
from this repository.

| Verified | Not yet verified |
| --- | --- |
| Unit and integration tests, run against fake backends (293 passing) | A local Ollama run (the `openai-compat` path has not been exercised end to end) |
| `ruff check .` and `ruff format --check .` | Whether the live model is accurate enough to trust on a real incident |
| Wheel builds and installs | |
| `SKILL.md` validated against the Agent Skills rules | |
| The offline eval harness, end to end with scripted replies (what CI runs) | |
| A full eval sweep with `gemma-4-26b-a4b-it` over all 8 fixtures: 100% top-1 category accuracy, 3 invented resource names (see below) | |
| One live end-to-end CLI call with `gemma-4-31b-it` on the `oom-exit-137` fixture, producing a correct `oom_killed` diagnosis (no retry needed) | |

A full sweep with the **Flash** variant of Gemma 4 (`gemma-4-26b-a4b-it`)
over all eight fixtures is recorded in [evals/results.md](evals/results.md). It
scores 100% top-1 category accuracy, but the hallucination check flags three
invented resource names — pod names that appear in the screenshot's `kubectl`
output but are absent from the text logs that make up the eval's input corpus:
`checkout-6f9c7d8b45-2xklm`, `frontend-84c6d7f5b-x9k2m`, and
`batch-worker-5d97b6c8f4-hm4pq`. These are not wrong categories, just names the
model transcribed from the image that the text-only hallucination heuristic
cannot attribute to the inputs. Judge the model on your own screenshots before
trusting it, and read the `Model:` line on every run: it records which model
actually produced the diagnosis.

## Ten-second demo

One fixture, end to end. The screenshot is the committed one from
`evals/fixtures/oom-exit-137/`.

```console
$ export GEMINI_API_KEY=... KUBE_DOCTOR_MODEL=<id from `kube-doctor - --list-models`>
$ kube-doctor evals/fixtures/oom-exit-137/screenshot.png \
    --logs evals/fixtures/oom-exit-137/logs.txt \
    --namespace analytics
# Kubernetes diagnosis
#
# Summary
# The batch worker is being OOM killed: the container is terminated with
# Reason OOMKilled and exit code 137, and the node logged a memory cgroup
# out-of-memory event. Its memory limit is 256Mi.
#
# Ranked causes
# 1. **Memory limit below the working set** — `oom_killed` (HIGH confidence)
#    - evidence: `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
#    - evidence: `Memory cgroup out of memory: Killed process 1 (java)`
#    - evidence: `Limits: memory: 256Mi`
#
# Next steps
# - [x] Confirm the limit and the last termination state (read-only)
#     - `$ kubectl describe pod batch-worker-5d97b6c8f4-hm4pq`
# - [x] Compare the limit with live usage (read-only)
#     - `$ kubectl top pod batch-worker-5d97b6c8f4-hm4pq --containers`
# - [ ] Raise the memory limit, or reduce the working set (MUTATING)
```

Two model calls, no cluster access, nothing executed. The exact wording depends
on the model; the structure and the cited evidence do not.

Record your own with `asciinema rec demo.cast` and convert to a GIF before
publishing — the recording is not committed here.

## Install

```bash
pip install -e .
```

Python 3.11+. The CLI is `kube-doctor`.

## Quickstart

### Gemini backend (default)

Model ids are **not hardcoded**, because they differ per API surface and per
release. List the ids your key can reach, then export the one you want:

```bash
export GEMINI_API_KEY=...          # https://aistudio.google.com/apikey
kube-doctor - --list-models        # prints the ids, including the open-weight Gemma ones
export KUBE_DOCTOR_MODEL=<id-from-that-list>
```

Then:

```bash
kube-doctor pod-crash.png --logs pod.log --namespace shop
```

`KUBE_DOCTOR_MODEL` has no default on purpose. A wrong default looks like a
broken tool; a clear error names the fix.

### Local OpenAI-compatible backend

```bash
ollama pull gemma4:e4b
export KUBE_DOCTOR_BASE_URL=http://localhost:11434/v1
export KUBE_DOCTOR_MODEL=gemma4:e4b
kube-doctor pod-crash.png --backend openai-compat
```

Vision support on local endpoints varies by model and by runtime. Gemma 4 images
need Ollama v0.20.0 or newer; older versions cannot serve `gemma4:e4b` at all.
**I have not tested this path** — no Ollama endpoint was available during
development, so the commands above are written from the documented Ollama API
rather than from a verified run. Smaller text-only models, and older Ollama
builds, will fail at the observe stage, and the CLI will say so rather than
guessing.

### Timeouts, retries, and the fallback model

Vision models are slow to start and the Gemini API returns `500`, `503`, and
`504` routinely while a model loads or a region saturates. Four settings handle
that, in this order:

| Setting | Default | Meaning |
| --- | --- | --- |
| `KUBE_DOCTOR_TIMEOUT` | `120` | Seconds per request, passed to the SDK as `HttpOptions(timeout=...)`. A vision model needs room. |
| Retries | 5 attempts | Retry `408`, `429`, `500`, `502`, `503`, `504`, socket errors, and timeouts. Jittered exponential backoff of roughly 2s, 4s, 8s, 16s, capped at 30s. |
| `Retry-After` | honoured | When the failing response sends one, it replaces the computed delay, clamped to 300s so a malformed header cannot park the CLI. |
| `KUBE_DOCTOR_FALLBACK_MODEL` | unset (off) | A second model tried **once**, only after the primary exhausts its retries on a retryable error. |

A `400`, `401`, `403`, or `404` is never retried and never falls back: another
model would reject the same request for the same reason, so the error reaches
you immediately.

```bash
export KUBE_DOCTOR_TIMEOUT=180
export KUBE_DOCTOR_FALLBACK_MODEL=<smaller-model-id>
kube-doctor pod-crash.png --timeout 180 --fallback-model <smaller-model-id>
```

The fallback is **off by default**. Enable it only if you have decided a failed
diagnosis is worse than an unexpected one, and read this first:

- **The fallback model is not required to be open-weight.** The primary is
  expected to be a Gemma release you chose; the fallback is whatever id you
  name, and that id decides who sees your data.
- **Your screenshot is sent to the fallback model.** The same pixels and logs go
  to it. Log text goes through the redaction pass first, but pixels are not
  redacted at all (see the security model), so anything visible in your terminal
  screenshot is visible to the second provider. That is still your cluster's
  output leaving your machine. If that is not acceptable, leave the fallback
  unset and let the run fail loudly.
- **Eval runs refuse to publish a mixed result.** If any case in
  `python evals/run_evals.py` needed the fallback, the numbers describe a
  mixture of models rather than the one being evaluated, so the harness prints
  them, explains why, and leaves `evals/results.md` untouched. Published
  numbers must come from the primary model alone.

When the fallback does answer, the diagnosis says which model produced it, on
stderr and in the `Model:` line, because a diagnosis from a different model is
not the one you asked for.

### Options

| Option | Meaning |
| --- | --- |
| `IMAGE` | Path to a screenshot, or `-` to read it from stdin |
| `--logs PATH` | Log file to consider alongside the screenshot |
| `--namespace TEXT` | Namespace the screenshot is about |
| `--context TEXT` | Free text: what changed, what you already tried |
| `--backend` | `gemini` (default) or `openai-compat` |
| `--format` | `markdown` (default) or `json` |
| `--model`, `--base-url` | Override `KUBE_DOCTOR_MODEL` / `KUBE_DOCTOR_BASE_URL` |
| `--timeout SECONDS` | Override `KUBE_DOCTOR_TIMEOUT` |
| `--fallback-model ID` | Override `KUBE_DOCTOR_FALLBACK_MODEL` |
| `--list-models` | List the model ids the API key can reach, then exit |
| `--verbose` | Log the intermediate observations to stderr |

`--verbose` turns on this tool's own logging only. `httpx`, `httpcore`, and
`google_genai` stay pinned at `WARNING` even then, so verbose output is the
observations and the retry notices, not SDK wire chatter.

Exit codes are part of the contract, because the Agent Skill script and the
eval harness branch on them: `0` ok, `2` bad input, `3` backend error,
`4` the model's output did not validate even after one repair.

## Output

```markdown
# Kubernetes diagnosis

*Model: `gemma-4-26b-a4b-it`*

## Summary

The `checkout` container exits immediately with code 1 because `DATABASE_URL` is
not present in the mounted configuration.

## Ranked causes

1. **Missing DATABASE_URL at startup** — `crash_loop_app_error` (HIGH confidence)
   - evidence: `Last State: Terminated, Reason: Error, Exit Code: 1`
   - evidence: `FATAL configuration error: DATABASE_URL is not set`

## Next steps

- [x] **Read the previous container's logs** (read-only)
     - `$ kubectl logs checkout-6f9c7d8b45-2xklm --previous --tail=200`
     - why: the newest logs only show the restart; the real exception is in the previous instance.
- [ ] **Correct the environment variable in the ConfigMap** (MUTATING)
     - why: the ConfigMap defines `DB_URL`; the Deployment expects `DATABASE_URL`.

## Missing info

- the container's full `env` list
```

`--format json` emits the whole run: diagnosis, observations, redaction counts,
and which stages ran.

## Evals

Eight fixtures, six failure modes and two negatives, all committed under
`evals/fixtures/`. Each holds a generated terminal or dashboard screenshot, the
log excerpt, the broken manifest it came from, and an `expected.json` with the
category that must be produced, keywords that must appear, and claims that must
not.

```bash
python evals/make_fixtures.py                  # regenerate the PNGs (needs a monospace font)
python evals/run_evals.py --backend gemini     # real run
python evals/run_evals.py --backend fake       # offline harness check, for CI
```

Scored per case: top-1 category accuracy, top-3 hit, keyword recall, a
hallucination check (every resource-shaped name in the output must also appear in
the inputs), and a safety check (zero mutating commands labelled `read_only`).
The run writes `evals/results.md` and exits non-zero below `--threshold`.

Each case records the model that actually answered it. If any case needed
`KUBE_DOCTOR_FALLBACK_MODEL`, the numbers describe a mixture of models rather
than the one you are evaluating, so the harness prints them, explains why, and
leaves `evals/results.md` untouched. Published results have to come from the
primary model alone.

See `evals/results.md` for the recorded Flash numbers. CI runs only the scripted
offline backend (`--backend fake`): it exercises the harness and the scoring,
and says nothing about model accuracy.

To add a case, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Security model

- **Redaction before the model call.** Log text is masked for bearer and basic
  auth headers, AWS access key ids, JWTs, passwords in URLs, private key blocks,
  `password=`/`token=` style pairs, and base64 blobs over 40 characters — the
  shape of encoded `Secret` data. The count of redactions is reported.
- **Pixels are not redacted.** A secret visible in a screenshot stays visible.
  This is the most important limitation in this README. Check the image before
  sharing it anywhere public.
- **No cluster access.** There is no Kubernetes client, no kubeconfig handling,
  and no network call to any API server in this codebase.
- **No command execution.** Suggested commands are strings. They are linted,
  labelled, and printed.
- **Safety lint.** Every suggested command is parsed. State-changing commands are
  forced to `mutating`. Anything that is not a recognised read-only `kubectl`,
  `helm`, or shell inspection command is dropped; commands that would print Secret
  material (`kubectl get secret -o yaml`) are dropped as sensitive. The
  description survives so the advice is not silently lost.
- **Honest failure.** If the image is illegible and there are no logs, the tool
  returns category `unknown` with a specific `missing_info` list instead of a
  guess.

## Limitations

- Screenshot legibility is the ceiling. A downscaled, blurred, or cropped panel
  cannot be diagnosed, and the tool says so instead of inventing a cause.
- Charts are described approximately: axis, units, trend shape, rough values.
- Failure modes are limited to the nine in the taxonomy
  (`oom_killed`, `crash_loop_app_error`, `image_pull`, `unschedulable`,
  `config_error`, `probe_failure`, `resource_throttling`, `network_dns`,
  `unknown`). PV, storage, RBAC, admission, and networking-policy bugs land in
  `config_error` or `network_dns` unless you add the mode.
- No cluster state, so it cannot confirm a cause, only rank it from evidence.
- Redaction is pattern-based and will miss shapes it does not know.
- Local vision quality depends entirely on the model you serve.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), including how to add a failure mode and
how to add an eval case.

## License

Apache-2.0. See [LICENSE](LICENSE).
