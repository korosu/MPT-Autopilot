# MPT-Autopilot Dockerfile
FROM python:3.12-slim-bookworm

WORKDIR /app

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PATH="/root/.local/bin:$PATH"

# Install MPT-Autopilot and its dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy source
COPY . .

# Default command: show help
ENTRYPOINT ["uv", "run", "mpt"]
CMD ["--help"]
