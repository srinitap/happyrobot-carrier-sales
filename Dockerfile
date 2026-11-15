# Backend for the HappyRobot inbound carrier sales agent.
# Build:  docker build -t carrier-sales-backend .
# Run:    docker run --env-file .env -p 8000:8000 carrier-sales-backend

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# the app itself 
COPY main.py tms_client.py negotiation.py ./

# run as a normal user
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]