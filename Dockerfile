FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
RUN useradd --create-home --uid 1000 appuser
USER appuser
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log"]
