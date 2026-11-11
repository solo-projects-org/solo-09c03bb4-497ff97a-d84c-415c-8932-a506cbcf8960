FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV DATA_DIR=/data
EXPOSE 8000

# 单 worker 多线程：SQLite 写入串行化，配合唯一约束保证幂等
CMD ["gunicorn", "--chdir", "app", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "app:app"]
