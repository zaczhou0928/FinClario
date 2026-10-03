FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app
COPY requirements-local.txt .
RUN pip install --no-cache-dir -r requirements-local.txt && useradd -m -u 1000 finclario
COPY --chown=finclario:finclario . .
RUN mkdir -p /app/runtime && chown finclario:finclario /app/runtime
USER finclario
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
