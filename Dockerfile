FROM ghcr.io/astral-sh/uv:0.11.21-python3.11-trixie-slim

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy dependency files and required project metadata first
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY libs/ ./libs/

# Sync workspace dependencies
RUN uv sync --frozen --no-dev --extra all

# Copy tools and application code
COPY tools/ ./tools/
COPY examples/ ./examples/

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENV ARCADE_SERVER_TRANSPORT=http \
    ARCADE_SERVER_HOST=0.0.0.0 \
    ARCADE_SERVER_PORT=8000 \
    PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "arcade_mcp_server", "--host", "0.0.0.0", "--port", "8000"]
