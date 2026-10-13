#!/usr/bin/env bash
# Thin wrapper around `kube-doctor` for the Agent Skill.
#
# Checks the environment up front so the agent gets a precise error instead of a
# Python traceback, then forwards every argument untouched. Exit codes are the
# CLI's own: 0 ok, 2 bad input, 3 backend error, 4 invalid model output.
set -euo pipefail

die() {
  printf '%s\n' "$*" >&2
  exit "${2:-3}"
}

if ! command -v kube-doctor >/dev/null 2>&1; then
  die "kube-doctor is not on PATH. Install it with: pip install kube-snapshot-doctor" 3
fi

backend="${KUBE_DOCTOR_BACKEND:-gemini}"
case "$backend" in
  gemini)
    [ -n "${GEMINI_API_KEY:-}" ] || die "GEMINI_API_KEY is not set. Export your Gemini API key." 3
    ;;
  openai-compat)
    [ -n "${KUBE_DOCTOR_BASE_URL:-}" ] ||
      die "KUBE_DOCTOR_BASE_URL is not set. Point it at your local endpoint, e.g. http://localhost:11434/v1" 3
    ;;
  *)
    die "unknown KUBE_DOCTOR_BACKEND '$backend'. Use 'gemini' or 'openai-compat'." 3
    ;;
esac

[ -n "${KUBE_DOCTOR_MODEL:-}" ] ||
  die "KUBE_DOCTOR_MODEL is not set. Model ids are not hardcoded: run 'kube-doctor - --list-models' to list the ids your key can reach, then export the one you want." 3

[ "$#" -gt 0 ] || die "usage: diagnose.sh IMAGE [--logs PATH] [--namespace NS] [--context TEXT]" 2

args=()
for arg in "$@"; do
  args+=("$arg")
done

if [ "${args[0]}" = "-" ]; then
  exec kube-doctor --backend "$backend" "${args[@]:1}" <&0
fi
exec kube-doctor --backend "$backend" "${args[@]}"
