FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRODUCTS_DB_PATH=/data/products.db

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

RUN mkdir -p /data
COPY products-service/main.py products-service/store.py /app/products-service/

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "/app/products-service", "--host", "0.0.0.0", "--port", "8001"]
