FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY templates/ /app/templates/
RUN mkdir -p /app/static

EXPOSE 8585

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
