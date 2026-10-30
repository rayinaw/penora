FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV SESSIONS_FILE=/data/sessions.json
VOLUME /data
EXPOSE 9100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9100"]
