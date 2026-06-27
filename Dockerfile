FROM python:3.12-slim

ENV TZ=America/New_York
RUN ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime && echo "America/New_York" > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
RUN mkdir -p /app/static

EXPOSE 8585

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8585"]
