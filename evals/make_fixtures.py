#!/usr/bin/env python3
"""Regenerate the eval fixtures.

Each fixture is a terminal-style screenshot of a deliberately broken workload,
the log excerpt that goes with it, and an ``expected.json`` describing what a
correct diagnosis must and must not say. The manifests under each fixture are
the broken YAML they were created from, so anyone can rebuild the cluster and
take a real screenshot if they prefer.

The committed PNGs are generated rather than photographed so the fixtures are
reproducible on any machine and the diff of a fixture is reviewable. Run this
only when adding or changing a case:

    python evals/make_fixtures.py

It needs Pillow and a monospace TrueType font. If none is found it says where it
looked, because a blurry fixture would quietly weaken the whole eval.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

ROOT: Final[Path] = Path(__file__).resolve().parent
FIXTURES: Final[Path] = ROOT / "fixtures"

#: Where to look for a monospace font, in preference order.
FONT_CANDIDATES: Final[tuple[str, ...]] = (
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\CascadiaMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/Library/Fonts/AndaleMono.ttf",
)

#: Terminal look: near-black background, light text, dim prompt.
BACKGROUND: Final[tuple[int, int, int]] = (18, 20, 24)
FOREGROUND: Final[tuple[int, int, int]] = (222, 226, 232)
PROMPT_COLOR: Final[tuple[int, int, int]] = (120, 190, 140)
ERROR_COLOR: Final[tuple[int, int, int]] = (232, 128, 128)
HEADER_COLOR: Final[tuple[int, int, int]] = (150, 170, 200)
#: Character cell and padding, in pixels at the base font size.
CELL_WIDTH: Final[int] = 11
LINE_HEIGHT: Final[int] = 24
PADDING: Final[int] = 24
FONT_SIZE: Final[int] = 19


@dataclass
class Line:
    """One rendered terminal line."""

    text: str
    color: tuple[int, int, int] = FOREGROUND


@dataclass
class Case:
    """One eval fixture, defined as data so the generator stays declarative."""

    name: str
    title: str
    category: str
    namespace: str
    description: str
    broken_manifest: str
    screenshot: str
    logs: str
    expected: dict[str, object] = field(default_factory=dict)


def find_font() -> ImageFont.FreeTypeFont:
    """Load the first available monospace font, or explain what is missing."""
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), FONT_SIZE)
    msg = (
        "no monospace TrueType font found. Install DejaVu Sans Mono or Consolas, "
        f"or add its path to FONT_CANDIDATES. Looked in: {', '.join(FONT_CANDIDATES)}"
    )
    raise SystemExit(msg)


def render_screenshot(lines: list[Line], path: Path, font: ImageFont.FreeTypeFont) -> None:
    """Draw a terminal window to ``path``."""
    columns = max((len(line.text) for line in lines), default=40) + 2
    width = PADDING * 2 + columns * CELL_WIDTH
    height = PADDING * 2 + len(lines) * LINE_HEIGHT
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    # Title bar, so the model sees a window rather than floating text.
    draw.rectangle((0, 0, width, 34), fill=(32, 35, 41))
    for index, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        left = 16 + index * 20
        draw.ellipse((left, 12, left + 11, 23), fill=color)
    draw.text((84, 9), "terminal — kubectl", font=font, fill=HEADER_COLOR)

    y = PADDING + 22
    for line in lines:
        draw.text((PADDING, y), line.text, font=font, fill=line.color)
        y += LINE_HEIGHT
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


def kubectl_lines(cases: list[Line]) -> list[Line]:
    """Prefix each line with a coloured prompt, as a real terminal would."""
    return [
        Line(f"$ {line.text}", PROMPT_COLOR) if index % 2 == 0 else line
        for index, line in enumerate(cases)
    ]


def build_cases() -> list[Case]:
    """Return the fixture definitions."""
    return [
        Case(
            name="crash-loop-bad-env",
            title="checkout CrashLoopBackOff after a bad env var rename",
            category="crash_loop_app_error",
            namespace="shop",
            description=(
                "The application reads DATABASE_URL at startup, but the ConfigMap key was "
                "renamed to DB_URL in a previous release, so the process exits 1 on boot."
            ),
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: shop
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: app
          image: registry.example.com/checkout:2.4.0
          envFrom:
            - configMapRef:
                name: checkout-config   # the ConfigMap only defines DB_URL
""",
            screenshot="""\
$ kubectl -n shop get pods
NAME                          READY   STATUS             RESTARTS   AGE
checkout-6f9c7d8b45-2xklm     0/1     CrashLoopBackOff   9          6m12s
checkout-6f9c7d8b45-q7rtd     0/1     CrashLoopBackOff   9          6m12s

$ kubectl -n shop describe pod checkout-6f9c7d8b45-2xklm | tail -20
    State:      Waiting
      Reason:   CrashLoopBackOff
    Last State:  Terminated
      Reason:    Error
      Exit Code: 1
      Started:   Mon, 01 Feb 2026 09:14:22 +0000
      Finished:  Mon, 01 Feb 2026 09:14:22 +0000
    Ready:      False
    Restart Count: 9
Events:
  Type     Reason     Age   From               Message
  ----     ------     ----  ----               -------
  Normal   BackOff    45s   kubelet            Back-off restarting failed container app
""",
            logs="""\
2026-02-01T09:14:22.101Z INFO  starting checkout 2.4.0
2026-02-01T09:14:22.103Z INFO  loading configuration
2026-02-01T09:14:22.104Z FATAL configuration error: DATABASE_URL is not set
Traceback (most recent call last):
  File "/app/bootstrap.py", line 41, in <module>
    settings = load_settings()
KeyError: 'DATABASE_URL'
""",
            expected={
                "category": "crash_loop_app_error",
                "must_mention": ["DATABASE_URL", "CrashLoopBackOff", "exit code 1"],
                "forbidden_claims": ["out of memory", "image pull", "insufficient cpu"],
            },
        ),
        Case(
            name="oom-exit-137",
            title="batch worker OOMKilled, exit code 137",
            category="oom_killed",
            namespace="analytics",
            description=(
                "The worker's memory limit is 256Mi but the job holds a full partition in "
                "memory, so the kernel kills the container."
            ),
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: batch-worker
  namespace: analytics
spec:
  template:
    spec:
      containers:
        - name: worker
          image: registry.example.com/batch-worker:5.2.1
          resources:
            limits:
              memory: 256Mi
              cpu: "2"
""",
            screenshot="""\
$ kubectl -n analytics get pods
NAME                            READY   STATUS    RESTARTS   AGE
batch-worker-5d97b6c8f4-hm4pq    0/1     Running   14 (3m ago)   22m
batch-worker-5d97b6c8f4-vt6bn    0/1     Running   11 (4m ago)   22m

$ kubectl -n analytics describe pod batch-worker-5d97b6c8f4-hm4pq | grep -A6 'Last State'
    Last State:  Terminated
      Reason:    OOMKilled
      Exit Code:  137
      Started:   Mon, 01 Feb 2026 08:53:04 +0000
      Finished:  Mon, 01 Feb 2026 08:56:11 +0000
    Limits:
      cpu:     2
      memory:  256Mi
Events:
  Warning  Memory 3m  kubelet  Memory cgroup out of memory: Killed process 1 (java) total-vm:9.2GB
""",
            logs="""\
2026-02-01T08:53:05.220Z INFO  worker 5.2.1 starting, 4 partitions assigned
2026-02-01T08:55:58.771Z WARN  heap 92% used, gc pause 1.4s
2026-02-01T08:56:09.004Z INFO  partition 3 of 4 loaded
2026-02-01T08:56:11.330Z WARN  OutOfMemoryError: Java heap space
""",
            expected={
                "category": "oom_killed",
                "must_mention": ["OOMKilled", "137", "256Mi"],
                "forbidden_claims": ["crashloopbackoff", "imagepullbackoff", "failedscheduling"],
            },
        ),
        Case(
            name="image-pull-typo",
            title="ErrImagePull on a mistyped tag",
            category="image_pull",
            namespace="shop",
            description="The deployment references tag 1.14.2, which was never pushed; only 1.14.1 exists.",
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: storefront
  namespace: shop
spec:
  template:
    spec:
      containers:
        - name: web
          image: registry.example.com/storefront:1.14.2   # 1.14.2 was never pushed
          imagePullPolicy: IfNotPresent
""",
            screenshot="""\
$ kubectl -n shop get pods
NAME                          READY   STATUS             RESTARTS   AGE
storefront-5b7d9c4f88-r4n2k   0/1     ImagePullBackOff   0          3m41s
storefront-5b7d9c4f88-w8c7p   0/1     ImagePullBackOff   0          3m41s

$ kubectl -n shop describe pod storefront-5b7d9c4f88-r4n2k | tail -8
Events:
  Type     Reason     Age                From     Message
  ----     ------     ----               ----     -------
  Normal   Scheduled  3m                 default  Successfully assigned shop/storefront-5b7d9c4f88-r4n2k
  Normal   Pulling    3m                 kubelet  Pulling image "registry.example.com/storefront:1.14.2"
  Normal   Pulled     3m                 kubelet  Successfully pulled image "registry.example.com/storefront:1.14.2"
  Warning  Failed     2m (x4 over 2m)    kubelet  Failed to pull image "registry.example.com/storefront:1.14.2": manifest unknown
  Warning  Failed     40s                kubelet  Error: ErrImagePull
  Normal   BackOff    12s (x6 over 2m)   kubelet  Back-off pulling image
""",
            logs="""\
# no application logs: the container never started.
# last lines of the kubelet log for the node running this pod
2026-02-01T09:22:14.882Z kubelet  failed to pull and unpack image: manifest unknown
""",
            expected={
                "category": "image_pull",
                "must_mention": ["1.14.2", "manifest unknown", "ImagePullBackOff"],
                "forbidden_claims": ["oomkilled", "crashloopbackoff", "insufficient memory"],
            },
        ),
        Case(
            name="unschedulable-cpu",
            title="Pending: 0/2 nodes are available: insufficient cpu",
            category="unschedulable",
            namespace="payments",
            description="The rollout requests 4 CPUs; the only node offers 2 allocatable after system reservation.",
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ledger
  namespace: payments
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: ledger
          image: registry.example.com/ledger:3.1.0
          resources:
            requests:
              cpu: "4"
""",
            screenshot="""\
$ kubectl -n payments get pods
NAME                      READY   STATUS    RESTARTS   AGE
ledger-5f9c74d6b8-nq2vp   0/1     Pending   0          40s

$ kubectl -n payments describe pod ledger-5f9c74d6b8-nq2vp | tail -6
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  35s   default-scheduler  0/2 nodes are available: 2 Insufficient cpu.
  Normal   Scheduled         1s    default-scheduler  Successfully assigned payments/ledger-5f9c74d6b8-nq2vp

$ kubectl -n payments describe node worker-1 | grep -A5 Allocatable
Allocatable:
  cpu:                2
  memory:             6Gi
  pods:               110
""",
            logs="""\
# no application logs: the pod was never scheduled, so nothing started.
2026-02-01T09:31:02.551Z scheduler  FailedScheduling: 0/2 nodes are available: 2 Insufficient cpu.
""",
            expected={
                "category": "unschedulable",
                "must_mention": ["insufficient cpu", "FailedScheduling", "Pending"],
                "forbidden_claims": ["oomkilled", "imagepullbackoff", "probe"],
            },
        ),
        Case(
            name="missing-configmap",
            title="CreateContainerConfigError: configmap not found",
            category="config_error",
            namespace="shop",
            description="A ConfigMap was renamed in the last release, so the reference no longer resolves.",
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: search
  namespace: shop
spec:
  template:
    spec:
      volumes:
        - name: search-config
          configMap:
            name: search-settings   # renamed to search-settings-v2
      containers:
        - name: search
          image: registry.example.com/search:1.9.0
          volumeMounts:
            - name: search-config
              mountPath: /etc/search
""",
            screenshot="""\
$ kubectl -n shop get pods
NAME                       READY   STATUS                       RESTARTS   AGE
search-6b98c7f5d4-zq9tm     0/1     CreateContainerConfigError   0          55s

$ kubectl -n shop describe pod search-6b98c7f5d4-zq9tm | grep -i configmap
    Mounts:
      Volumes from  kube-api-access-xxxx:
    Events:
  Warning  Failed  40s  kubelet  Error: configmap "search-settings" not found
  Warning  Failed  30s  kubelet  Error: configmap "search-settings" not found

$ kubectl -n shop get configmap
NAME                 DATA   AGE
shopify-endpoints    1      9d
""",
            logs="""\
# no application logs: the container never started.
2026-02-01T09:33:11.004Z kubelet  Error: configmap "search-settings" not found
""",
            expected={
                "category": "config_error",
                "must_mention": ["search-settings", "not found", "CreateContainerConfigError"],
                "forbidden_claims": ["oomkilled", "insufficient cpu", "imagepullbackoff"],
            },
        ),
        Case(
            name="cpu-throttling-panel",
            title="Grafana: CPU throttling spike on the payments API",
            category="resource_throttling",
            namespace="payments",
            description=(
                "A dashboard panel shows container CPU pinned at its 500m limit with a sawtooth "
                "pattern; the container is not being killed, it is being slowed."
            ),
            broken_manifest="""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments-api
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: api
          image: registry.example.com/payments-api:4.6.2
          resources:
            limits:
              cpu: 500m
""",
            screenshot="""\
Grafana - Container CPU / Pod "payments-api-79d4c8f6b5-kx2mq"     namespace: payments
Query: rate(container_cpu_usage_seconds_total{namespace="payments"}[1m])
Panel: CPU usage vs limit (cores)

 1.0 ┤                              ┌──────┐        ┌──────┐
 0.9 ┤                              │      │        │      │
 0.8 ┤                         ┌────┘      └────────┘      └────
 0.7 ┤                    ┌────┘
 0.6 ┤               ┌────┘
 0.5 ┤  limit  ──────┼──────────────────┼──────────────────┼───  0.5
 0.4 ┤          ┌────┘                  ┌┘                  ┌┘
 0.3 ┤     ┌────┘
 0.2 ┤┌────┘
 0.1 ┼────────────────────────────────────────────────────────────
     09:00    09:10    09:20    09:30    09:40    09:50    10:00

  peak 0.98 cores, sustained at the 0.5 core limit, sawtooth every ~90s
  throttled_periods: 4820    throttled_time: 1.94e+10ns
  restarts: 0    Ready: 1/1    status: Running
""",
            logs="""\
2026-02-01T09:58:11.004Z INFO  payments-api 4.6.2 ready, listening on :8080
2026-02-01T09:58:42.771Z INFO  GET /v1/charges/8812 200 41ms
2026-02-01T09:59:03.118Z WARN  p99 latency 1840ms, cpu saturated at limit
""",
            expected={
                "category": "resource_throttling",
                "must_mention": ["0.5", "throttl"],
                "forbidden_claims": ["oomkilled", "imagepullbackoff", "crashloopbackoff"],
            },
        ),
        Case(
            name="negative-blurry",
            title="Negative: an unreadable screenshot",
            category="unknown",
            namespace="",
            description="A heavily blurred photograph; there is no evidence to work from.",
            broken_manifest="",
            screenshot="""\
$ kubectl -n prod get pods
NAME                      READY   STATUS   RESTARTS   AGE
a-7f9c                    0/1     ~~~~~~~~~   ~~~      ~~
b-8c4d                    0/1     ~~~~~~   ~~~      ~~~
c-2a1e                    0/1     ~~~~~~~~~  ~       ~

  (the photo is out of focus; status text and resource names are unreadable)
""",
            logs="",
            expected={
                "category": "unknown",
                "must_mention": [],
                "forbidden_claims": ["oomkilled", "imagepullbackoff", "unschedulable", "probe"],
                "negative": True,
            },
        ),
        Case(
            name="negative-not-kubernetes",
            title="Negative: a photo of a spreadsheet, not a cluster",
            category="unknown",
            namespace="",
            description="A spreadsheet screenshot; the model must say it is not Kubernetes-related.",
            broken_manifest="",
            screenshot="""\
Q3 budget forecast.xlsx - LibreOffice Calc

        A          B            C           D
1   Region    Revenue       Cost       Margin
2   EMEA      1,204,000     980,000     18.6%
3   APAC        842,300     701,500     16.7%
4   LATAM        318,900     295,100      7.5%
5   TOTAL     2,365,200   1,976,600     16.4%

6   (a spreadsheet, not a terminal and not a dashboard)
""",
            logs="",
            expected={
                "category": "unknown",
                "must_mention": [],
                "forbidden_claims": [
                    "oomkilled",
                    "crashloopbackoff",
                    "imagepullbackoff",
                    "unschedulable",
                ],
                "negative": True,
            },
        ),
    ]


def lines_for(case: Case) -> list[Line]:
    """Turn a case's screenshot text into styled terminal lines."""
    rendered: list[Line] = [Line(case.title, HEADER_COLOR), Line("")]
    for raw in case.screenshot.splitlines():
        line = Line(raw)
        if raw.startswith("$ "):
            line = Line(raw[2:], PROMPT_COLOR)
        elif any(
            word in raw.lower() for word in ("warning", "error", "failed", "fatal", "oomkilled")
        ):
            line = Line(raw, ERROR_COLOR)
        rendered.append(line)
    return rendered


def main() -> int:
    """Write every fixture to disk."""
    font = find_font()
    for case in build_cases():
        directory = FIXTURES / case.name
        directory.mkdir(parents=True, exist_ok=True)
        render_screenshot(lines_for(case), directory / "screenshot.png", font)
        (directory / "logs.txt").write_text(case.logs, encoding="utf-8")
        (directory / "expected.json").write_text(
            json.dumps(case.expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if case.broken_manifest:
            (directory / "broken-manifest.yaml").write_text(case.broken_manifest, encoding="utf-8")
        manifest = {
            "name": case.name,
            "title": case.title,
            "description": case.description,
            "namespace": case.namespace or "(none)",
            "screenshot": "screenshot.png",
            "logs": "logs.txt" if case.logs else None,
            "expected": "expected.json",
        }
        import yaml  # imported here so the module docstring stays accurate

        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False), encoding="utf-8"
        )
        print(f"wrote {directory.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
