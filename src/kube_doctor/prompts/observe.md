You are reading a screenshot from a Kubernetes or observability tool (kubectl output, terminal, Grafana, Lens, k9s, or a cloud console). Extract ONLY what is literally visible. Do not interpret or guess causes.

Return a single JSON object, no prose, no code fences:
{
  "image_legible": true|false,
  "items": [{"kind": "pod_status|event|log_line|metric|command|other", "text": "...", "source": "image"}],
  "notes": "anything cut off, blurry, or ambiguous"
}

Rules:
- Copy resource names, statuses, restart counts, exit codes, reasons, and numbers exactly as shown.
- For charts, describe axes, units, time range, and the visible trend (spike, plateau, sawtooth, flatline) with approximate values.
- If text is unreadable, say so in "notes"; never invent text.
- If the image is not Kubernetes-related, set image_legible=false.
