---
name: kube-snapshot-doctor
description: Diagnose a Kubernetes problem from a screenshot of failing pods, kubectl output, or a Grafana panel, plus optional logs, and return an evidence-backed cause list with read-only next steps. Use when the user shares a screenshot of a CrashLoopBackOff, Pending, ImagePullBackOff, OOMKilled, failing probe, or a throttled CPU chart and asks what is going wrong or how to fix it. Reads only; never connects to a cluster and never runs the commands it suggests.
license: Apache-2.0
compatibility: Requires Python 3.11+, the kube-doctor CLI on PATH, and either GEMINI_API_KEY plus KUBE_DOCTOR_MODEL for the Gemini backend or KUBE_DOCTOR_BASE_URL plus KUBE_DOCTOR_MODEL for a local OpenAI-compatible endpoint. macOS, Linux, and Windows.
metadata:
  version: "0.1.0"
  upstream: "https://github.com/AdiB1619/kube-snapshot-doctor"
---

# Kube Snapshot Doctor

Turn a screenshot of a failing workload into a ranked, cited diagnosis. Two
stages run behind the CLI: a vision stage that transcribes only what is visible,
then a text stage that reasons from that transcription, a failure-mode
reference, and any logs you supply.

## When to use

- The user pastes a screenshot of `kubectl get pods`, `kubectl describe`, a
  terminal, Grafana, Lens, k9s, or a cloud console and asks what is wrong.
- The user describes symptoms in a screenshot: CrashLoopBackOff, Pending,
  ImagePullBackOff, OOMKilled, failing probes, a throttled CPU panel.
- The user wants a written explanation with next steps to hand to someone else.

Do not use it to apply fixes. This tool never touches a cluster.

## Prerequisites

Check these before running, and tell the user exactly which one is missing:

1. `kube-doctor` on `PATH`. If not: `pip install kube-snapshot-doctor`.
2. A model. For the Gemini backend: `GEMINI_API_KEY` set and `KUBE_DOCTOR_MODEL`
   set to a model id your key can reach. To find the id, run
   `kube-doctor - --list-models`; it prints the ids, including the open-weight
   Gemma ones. Model ids are never hardcoded because they change.
3. For a local endpoint instead: `KUBE_DOCTOR_BASE_URL` (for example
   `http://localhost:11434/v1` for Ollama) and `KUBE_DOCTOR_MODEL`.

The wrapper script checks all of this and prints the missing variable by name.

## Workflow

1. **Collect the inputs.** You need the screenshot path. Ask for it if you do
   not have it. If logs exist, ask for the path too; logs raise confidence more
   than anything else, and secrets in them are redacted before the model call.
2. **Run the diagnosis.** Use the wrapper so the environment check is explicit:

   ```bash
   "${CLAUDE_SKILL_DIR}/scripts/diagnose.sh" /path/to/screenshot.png --logs /path/to/pod.log --namespace payments
   ```

   Use `kube-doctor` directly if the wrapper is unavailable:

   ```bash
   kube-doctor /path/to/screenshot.png --logs /path/to/pod.log --namespace payments --format markdown
   ```

3. **Read the exit code before the output.** It is part of the contract:

   | Code | Meaning | What to do |
   | --- | --- | --- |
   | 0 | Diagnosis produced | Report it |
   | 2 | Bad input: file missing, unreadable image, neither image nor logs | Fix the path, ask for the right file |
   | 3 | Backend error: no key, unknown model id, rate limit, empty reply | Fix the environment; suggest `kube-doctor - --list-models` |
   | 4 | Model output invalid even after one repair | Re-run once; if it repeats, treat the case as `unknown` and say the evidence was insufficient |

4. **Report the diagnosis**, leading with the summary and the top cause. Keep
   the evidence quotes: they are the reason to trust the answer.
5. **Offer the next step** the user asked for. Stop there.

## How to interpret the output

The Markdown output has four sections:

- **Summary** — two sentences in plain language. Quote it or paraphrase it; do
  not replace it with your own theory.
- **Ranked causes** — at most three, each with a category, a confidence, and
  evidence copied from the input. The top cause is the model's best-supported
  answer; the rest are alternatives worth keeping in mind.
- **Next steps** — a checklist ordered read-only first. Read-only steps are safe
  to run and often answer the question on their own. `MUTATING` steps change
  cluster state.
- **Missing info** — what would raise confidence. If this list is long, ask for
  one item from it rather than guessing.

Special cases:

- Category `unknown` with a non-empty **Missing info** means the tool declined to
  guess. Say so plainly and ask for one of the listed items. Do not invent a
  cause to fill the gap.
- A step whose command reads "command withheld by the safety lint" was dropped
  because it read Secret material or was not a recognised read-only command.
  Suggest the safe alternative rather than reconstructing the withheld command.
- `repaired` in the metadata means the model's first reply failed validation and
  was corrected once. The result is valid, but say the model needed a repair if
  the user is judging reliability.

## Rules

- **Never run a command marked `MUTATING` without explicit user approval in that
  turn.** Read-only commands still need permission in most environments; the
  difference is that the user is authorizing a state change, not just a read.
- **Never invent a cause the tool did not report.** If the tool says `unknown`,
  the answer is `unknown` plus a request for better evidence.
- **Never ask for, or echo, Secret contents.** Do not run `kubectl get secret
  -o yaml`. The safety lint already withholds those commands; do not work around
  it.
- **The tool has no cluster access**, so it cannot verify anything. Phrase results
  as "based on this screenshot", not "the pod is".
- **Redaction is best effort.** Text secrets in logs are masked before the model
  call; pixels in the screenshot are not. Remind the user to check the image
  before sharing it publicly.

## Failure modes

See `references/failure-modes.md` for the visible signals, confirmation steps,
and typical fixes for each of the nine categories. Read it when you need to
explain a category in depth, or when the tool's top cause looks thin and you
want to check the alternatives against the evidence yourself.
