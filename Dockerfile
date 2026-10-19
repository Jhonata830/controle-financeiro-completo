FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY templates /app/templates
COPY static /app/static

RUN mkdir -p /app/dados /app/backups

EXPOSE 8000

CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "60", "--bind", "0.0.0.0:8000", "app:app"]
