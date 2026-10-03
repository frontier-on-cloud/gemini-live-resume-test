# Round 3 (real packet loss): the harness in a Linux container, where iptables can drop
# one TCP flow silently. Same Python and locked dependencies as rounds 1 and 2 (uv.lock).
# The .env is never copied into the image: mount it read-only at run time.
# Stage 2 adds recovery.py (client-side recovery, scenarios BR1/BR2).
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iptables iproute2 tcpdump procps \
 && apt-get clean
RUN pip install --no-cache-dir uv==0.12.3
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY resume_test.py recovery.py freeze_proxy.py blackhole.py test_blackhole.py ./
COPY assets ./assets
