# Kube Snapshot Doctor — public demo image (Gemma 4 via Gemini API).
# No cluster access, no command execution, no secrets baked in.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Install runtime + UI dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
COPY skills/kube-snapshot-doctor/references/ skills/kube-snapshot-doctor/references/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[ui]"

# Copy the rest (CLI, prompts, skills, evals, tests).
COPY . .

EXPOSE 7860

# Unified entry point: landing page at / plus Gradio workspace at /workspace.
# Reads PORT from the environment (platform default 7860).
CMD ["python", "-m", "kube_doctor.web"]
