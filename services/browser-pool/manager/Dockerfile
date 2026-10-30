FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pool_manager ./pool_manager
CMD ["uvicorn", "pool_manager.main:app", "--host", "0.0.0.0", "--port", "8000"]
