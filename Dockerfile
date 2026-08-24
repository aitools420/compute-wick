FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/compute.db
EXPOSE 8956
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8956"]
