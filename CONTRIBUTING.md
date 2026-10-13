# Contributing

Apache-2.0. Type hints everywhere, pydantic v2 for schemas, typer for the CLI,
pytest for tests, ruff for lint and format. Keep modules small and testable.
Report the test and lint results with every change.

```bash
python -m pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

CI runs lint, the test suite on Python 3.11/3.12/3.13, the offline eval harness,
the `SKILL.md` validation, and a credential scan over the whole git history.

## Adding a new failure mode

A failure mode has five touch points. Skipping any of them leaves the feature
half-wired, which is worse than not adding it.

**1. The enum.** Add the category to `Category` in `src/kube_doctor/schema.py`
with a lowercase value, e.g. `PV_BIND_FAILED = "pv_bind_failed"`.

**2. The reference.** Add a section to
`skills/kube-snapshot-doctor/references/failure-modes.md` using the same shape
as the existing ones:

```markdown
## pv_bind_failed

- **Visible signals:** ...the literal strings and reasons that appear in output...
- **Confirm:** ...how to tell it from the neighbouring modes...
- **Typical causes:** ...
- **Read-only next steps:** ...
- **Fix shape:** ...
```

The rule that matters: **visible signals first**. The model reads this on every
call and will cite it, so every line has to be something an operator could point
at in a screenshot. Do not write "the volume might be misconfigured"; write
`persistentvolumeclaim "data-db-0" not found`.

**3. The prompts.** Add the category name to the allowed list in
`src/kube_doctor/prompts/diagnose.md` and in `prompts/observe.md` if the mode
needs a distinct chart or event vocabulary.

**4. The tests.** Extend the table-driven cases in `tests/test_schema.py` and, if
the mode has a command shape worth linting, `tests/test_safety.py`.

**5. An eval case.** See below. A mode with no fixture will silently rot.

## Adding an eval case

1. Decide the case: a real broken workload, a namespace, the namespace, the log
   excerpt you would actually paste.
2. Add it to `build_cases()` in `evals/make_fixtures.py` — a `Case` with the
   terminal text, the logs, the broken manifest, and the `expected` dict
   (`category`, `must_mention`, `forbidden_claims`, and `negative: true` for a
   case that must return `unknown`).
3. Run `python evals/make_fixtures.py` to regenerate the PNGs, manifest, logs,
   and `expected.json`.
4. Run the harness with a real backend and paste the table into `evals/results.md`.
5. Read the hallucination column. If it flags a name, either the name is genuinely
   invented (fix the prompt or the reference) or the allowlist in
   `evals/run_evals.py` needs the word (for example a Grafana panel title).

Rules for expectations:

- `must_mention` holds strings a correct answer must contain. Keep them to things
  visible in the input: a reason, an exit code, a configmap name, a limit.
- `forbidden_claims` holds the *other* modes' vocabulary. This is what catches a
  model that names a plausible-sounding wrong cause.
- Negative cases must have `category: unknown`, empty `must_mention`, and
  `negative: true`.

## Screenshots

Fixtures are generated, not photographed, so they are reproducible and the diff
is reviewable. `make_fixtures.py` needs a monospace TrueType font (Consolas,
Cascadia Mono, Menlo, DejaVu Sans Mono, or Liberation Mono); it prints the paths
it tried if none is found. If you would rather use a real screenshot, drop the PNG
into the fixture directory, delete the generator entry so it is not overwritten,
and keep the `expected.json`.

Do not use real company or customer data. Everything in `evals/fixtures/` should
be reproducible from a local kind or minikube cluster and the broken manifests
committed alongside.

## Tests

- No network. Live backend tests are opt-in behind `KUBE_DOCTOR_LIVE=1` and real
  credentials; everything else uses a fake backend.
- No cluster. Nothing in this repo talks to a Kubernetes API server.
- Prefer table-driven parametrised tests for anything with a list of cases.
- Every validator gets a test, including the case that must fail.

## Secrets

Never commit a real credential. `tests/check_no_secrets.py` scans the working tree
and the full git history for credential shapes in CI. If it fires, remove the
value and rewrite the history rather than deleting the line in the newest commit.
