# Hosted MCP server for mcp.buildingactivity.com: streamable HTTP, each caller's own key.
# docker build -t buildingactivity-mcp . && docker run -p 8765:8765 buildingactivity-mcp
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps -w /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BUILDINGACTIVITY_API_URL=https://api.buildingactivity.com \
    BUILDINGACTIVITY_MCP_ALLOWED_HOSTS=mcp.buildingactivity.com \
    FORWARDED_ALLOW_IPS=127.0.0.1
COPY deploy/requirements-mcp.txt /tmp/requirements.txt
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps /wheels/*.whl \
 && rm -rf /wheels /tmp/requirements.txt \
 && useradd --system --uid 10001 --no-create-home mcp
USER 10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status != 200)"]
# No BUILDINGACTIVITY_API_KEY here, ever: every request must bring its caller's key.
ENTRYPOINT ["buildingactivity-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
