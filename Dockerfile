FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system workflow \
    && useradd --system --gid workflow --home-dir /app workflow \
    && mkdir -p /data /app/workflow/logs \
    && chown -R workflow:workflow /data /app

COPY workflow/requirements.txt /app/workflow/requirements.txt
RUN pip install --no-cache-dir -r /app/workflow/requirements.txt

COPY workflow/*.py /app/workflow/
COPY workflow/sample_data /app/workflow/sample_data
COPY workflow/sample_docs /app/workflow/sample_docs

USER workflow
EXPOSE 8001 8501

CMD ["uvicorn", "workflow.api:app", "--host", "0.0.0.0", "--port", "8001"]
