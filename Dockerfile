# email-mcp HTTP service (the stdio MCP install is unaffected; this image only runs
# the multi-tenant web service). Built from the launchlab compose file.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY config/accounts.example.yml config/
COPY src ./src
RUN pip install --no-cache-dir ".[http]"

# State (SQLite DB, attachment downloads) lives on the mounted /data volume.
ENV EMAIL_MCP_DB=/data/state.db \
    EMAIL_MCP_DOWNLOAD_DIR=/data/attachments \
    EMAIL_MCP_HTTP_HOST=0.0.0.0 \
    EMAIL_MCP_HTTP_PORT=8000

EXPOSE 8000
CMD ["email-mcp-http"]
