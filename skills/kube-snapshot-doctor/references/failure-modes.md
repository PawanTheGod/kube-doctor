# Kubernetes failure-mode reference

Injected into the DIAGNOSE prompt. Keep it compact: the model reads it on every
call, so every line has to earn its tokens. Cite the visible signal for each
mode; the model must not use this list to invent causes that are not present.

## oom_killed

- **Visible signals:** `Reason: OOMKilled`; `Last State: Terminated, Exit Code: 137`; restart count
  climbing with a steady `Ready 0/1`; `Memory cgroup out of memory` in `kubectl describe`; events
  repeating `Memory cgroup out of memory: Killed process`.
- **Confirm:** the container's `limits.memory` versus its observed working set; exit code 137 is
  SIGKILL and is *not* unique to OOM, so pair it with the reason or the cgroup line.
- **Typical cause:** the limit is below real usage, often after a traffic or data-volume increase.
- **Read-only next steps:** `kubectl describe pod <pod>` (limits and last state),
  `kubectl top pod <pod> --containers`, node memory pressure events.
- **Fix shape:** raise `resources.limits.memory`, or reduce the application's footprint.

## crash_loop_app_error

- **Visible signals:** `CrashLoopBackOff`; restart count increasing over minutes; `Exit Code: 1`,
  `2`, or `255`; `Last State: Terminated, Reason: Error`; an application stack trace in logs.
- **Confirm:** `kubectl logs <pod> --previous` shows the previous instance's output, which is where
  the actual exception usually is; the newest logs may only show the restart.
- **Typical causes:** missing or misspelled environment variable, failed database connection, an
  unhandled exception at startup, a config file the app cannot parse.
- **Read-only next steps:** `kubectl logs <pod> --previous --tail=200`, `kubectl describe pod <pod>`,
  `kubectl get cm <name> -o name` to confirm the ConfigMap exists.
- **Fix shape:** correct the env var or config, or fix the application error.

## image_pull

- **Visible signals:** `ImagePullBackOff` or `ErrImagePull`; `Failed to pull image "x:1.2.3"`;
  `manifest unknown`; `not found` for a tag; `ImagePullBackOff` on every replica.
- **Confirm:** the exact image reference including the tag; a missing `imagePullSecrets` for a
  private registry; a `latest` tag that no longer exists in the registry.
- **Typical causes:** typo in the tag, deleted image, wrong registry hostname, missing pull secret,
  expired registry credentials, air-gapped node that cannot reach the registry.
- **Read-only next steps:** `kubectl describe pod <pod>` (events carry the registry's message),
  `kubectl get pod <pod> -o yaml` (image and `imagePullSecrets`).
- **Fix shape:** correct the image reference, or create the missing pull secret.

## unschedulable

- **Visible signals:** `Pending` pods; events `0/N nodes are available: insufficient cpu`,
  `insufficient memory`, `node(s) had untolerated taint`, `pod has unbound immediate
  PersistentVolumeClaims`; `FailedScheduling` repeated in `kubectl describe`.
- **Confirm:** the pod's `requests` versus allocatable on the candidate nodes; taints and tolerations;
  whether the PersistentVolumeClaim is `Pending`; node selectors and affinity that match nothing.
- **Typical causes:** requests above every node's allocatable, an unsatisfiable node selector, a
  missing toleration for a node taint, an unbound PVC, or a full topology zone.
- **Read-only next steps:** `kubectl describe pod <pod>`, `kubectl describe node <node>` (Allocatable
  and taints), `kubectl get pvc`, `kubectl get nodes --show-labels`.
- **Fix shape:** lower requests, widen the selector, add a toleration, or provision capacity.

## config_error

- **Visible signals:** `CreateContainerConfigError`; `configmap "x" not found`; `secret "y" not
  found`; `Error: couldn't find key`; a crash at start with a message naming a missing env var;
  a pod stuck in `ContainerCreating`.
- **Confirm:** the `env`/`envFrom`/`volume` references versus what actually exists in the namespace.
  ConfigMaps and Secrets are namespace-scoped, so a name that exists elsewhere still fails here.
- **Typical causes:** a renamed or deleted ConfigMap/Secret, a wrong key inside an existing
  ConfigMap, a mount path that does not exist, or a reference to the wrong namespace.
- **Read-only next steps:** `kubectl get cm,secret` in the namespace (names only, never `-o yaml`
  on a Secret), `kubectl describe pod <pod>`.
- **Fix shape:** create or correct the referenced object in the same namespace.

## probe_failure

- **Visible signals:** `Ready 0/1` while the container is `Running` and restarting is low; events
  `Liveness probe failed: ...` or `Readiness probe failed: ...`; `Unhealthy` in describe output.
- **Confirm:** the probe's `path`/`port`/`command` against what the app actually serves; whether the
  failure started at a specific rollout; `initialDelaySeconds` versus real start-up time.
- **Typical causes:** the app binds to `127.0.0.1` instead of `0.0.0.0`, the probe path returns a
  non-2xx, start-up is slower than the initial delay, or the probe port is not exposed.
- **Read-only next steps:** `kubectl describe pod <pod>` (probe failure messages), `kubectl logs
  <pod> --tail=50`, `kubectl get pod <pod> -o yaml` (probe spec).
- **Fix shape:** correct the probe target or timing. This is rarely a real outage: the container is
  alive, so treat it as a traffic problem rather than a crash.

## resource_throttling

- **Visible signals:** a CPU panel that sits at a flat ceiling with `cpu: 95-100%`; Grafana or
  `kubectl top` showing CPU pinned at the limit while latency climbs; a sawtooth pattern matching
  the throttling period.
- **Confirm:** the container's `limits.cpu` versus its usage; Go runtimes show an explicit
  `throttled` message; Java shows GC pressure alongside capped CPU.
- **Typical causes:** the limit is set too low for the workload, or a hot loop plus a tight limit.
- **Read-only next steps:** `kubectl top pod <pod> --containers`,
  `kubectl get pod <pod> -o jsonpath` for the CPU limit, node-level CPU panel.
- **Fix shape:** raise the CPU limit, or fix the hot loop. Unlike `oom_killed`, the container is not
  being killed; it is being slowed down.

## network_dns

- **Visible signals:** log lines such as `connection refused`, `i/o timeout`, `no such host`,
  `context deadline exceeded`; `CreateContainerError` with `failed to create pod sandbox`;
  probes failing only from some nodes; DNS resolution errors naming a short name that is not
  fully qualified.
- **Confirm:** whether the name is missing the namespace suffix (`service` vs
  `service.namespace.svc.cluster.local`); whether failures are node-specific (DNS or CNI on one
  node) or everywhere (wrong port, wrong service name, NetworkPolicy).
- **Typical causes:** wrong service name or port, missing FQDN, CoreDNS unhealthy, a NetworkPolicy
  blocking egress, or a `hostPort` conflict.
- **Read-only next steps:** `kubectl get svc,endpoints`, `kubectl get networkpolicy`,
  `kubectl logs -n kube-system -l k8s-app=kube-dns`, `dig <service>.<ns>.svc.cluster.local` from a
  debug pod, and the affected pod's events.
- **Fix shape:** correct the name/port, or open the network path.

## unknown

- **When to use it:** the image is illegible, the screenshot is not Kubernetes-related, the logs are
  empty, or nothing in the inputs matches any mode above. Never use it to avoid ranking a cause you
  can support; use it when the evidence genuinely does not support one.
- **What to return:** category `unknown`, low confidence, and a specific `missing_info` list naming
  exactly what to capture next (for example "the `kubectl describe pod` event lines" or "a
  re-screenshot at full resolution").
