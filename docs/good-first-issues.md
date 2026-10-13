# Good first issues

Ready to file as GitHub issues with the `good first issue` label. Each is
scoped to one file, has an explicit acceptance check, and can be finished without
a cluster or an API key.

---

### 1. Add a new redaction rule for a secret shape we do not cover yet

**Labels:** `good first issue`, `enhancement`, `security`

**Where:** `src/kube_doctor/redact.py`, `tests/test_redact.py`

The redactor currently handles bearer/basic tokens, AWS access key ids, JWTs,
passwords in URLs, private key blocks, `password=`/`token=` pairs, and long base64
blobs. Real logs contain other shapes. Kubernetes and cloud ecosystems in
particular produce: GCP service account JSON, Azure storage connection strings,
npm and PyPI tokens, Slack webhooks, and generic Kubernetes ServiceAccount token
files.

Pick one shape and add it:

1. Add a `Redaction` entry to `REDACTIONS` in `REDACTIONS`-ordering terms: a more
   specific shape must come before the generic `base64` rule, or it will be eaten
   first. The `name` field is the `[REDACTED:<name>]` marker, so choose it to read
   well in the output.
2. Add a case to the `REDACTION_CASES` table in `tests/test_redact.py` with the
   real-world string, the substring that must not survive, and the expected type.
3. Add a case to `CLEAN_CASES` if your pattern could plausibly match ordinary
   Kubernetes text. Prove it does not.

**Done when:** `pytest tests/test_redact.py` passes and the new case is in the
table, and `ruff check .` is clean.

---

### 2. Add a read-only inspection command to the safety lint allowlist

**Labels:** `good first issue`, `enhancement`, `safety`

**Where:** `src/kube_doctor/safety.py`, `tests/test_safety.py`

`lint_command` classifies a suggested command into `allowed` (read-only),
`allowed` (mutating), `sensitive`, or `rejected`. A handful of genuinely
read-only commands are currently rejected because they are not in
`READ_ONLY_VERBS` or `READ_ONLY_SHELL` — for example `kubectl get secret -o
name` variants that list only names, `kubectl api-resources`, `kubectl cluster-info
dump` (redact it first), `kubectl get --raw /healthz`, `ethtool`, `ip addr`,
`tc -s qdisc`, `ethtool -S`, and `kubectl get volumeattachments`.

Add the ones you can justify:

1. Add the subcommand to `READ_ONLY_VERBS`, or the program to `READ_ONLY_SHELL`.
2. Add its case to the `COMMAND_CASES` table in `tests/test_safety.py` with the
   verdict and risk you expect.
3. If it exposes credential material, add a `SENSITIVE_PATTERNS` entry instead
   and expect `Verdict.SENSITIVE`.

**Done when:** `pytest tests/test_safety.py` passes, and for anything you added
you can say in one sentence why it cannot change cluster or host state.

---

### 3. Add an eval fixture for a failure mode variant we do not cover

**Labels:** `good first issue`, `good first issue - Hacktoberfest`, `evals`

**Where:** `evals/make_fixtures.py`, `evals/fixtures/`

The eight committed fixtures cover one variant per category. Real incidents have
variants that read differently: an OOM shown only as a Grafana memory panel
rather than a `describe` dump, a DNS failure that only affects one node, a probe
failing after a rollback, a `Pending` pod caused by an untolerated taint.

Add one:

1. Add a `Case` to `build_cases()` in `evals/make_fixtures.py` with the terminal
   text, the logs, the broken manifest, and the `expected` dict.
2. Run `python evals/make_fixtures.py` and commit the generated PNG, `logs.txt`,
   `manifest.yaml`, and `expected.json`.
3. If the mode needs a new visible-signal vocabulary, extend
   `references/failure-modes.md` as well.

**Rules:** `must_mention` must be strings visible in the input (a reason, an exit
code, a name, a limit). `forbidden_claims` must be the *other* modes' vocabulary.
No real company or customer data.

**Done when:** `python evals/run_evals.py --backend fake --threshold 1.0` still
passes, and `python evals/run_evals.py --backend gemini` reports the new case with
the model id you used.
