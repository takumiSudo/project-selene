FROM python:3.12-slim AS base
RUN pip install --no-cache-dir fastapi uvicorn
RUN mkdir -p /rover/output
COPY base/server.py /rover/server.py
WORKDIR /rover
EXPOSE 8080

FROM base

# --- Dependencies ---
RUN pip install --no-cache-dir \
    pydantic==2.* \
    httpx \
    networkx \
    anthropic

# --- Copy your code ---
COPY . /rover/

# Make scripts executable
RUN chmod +x /rover/run_mapping.sh /rover/run_reporting.sh

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
