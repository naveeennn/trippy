# Runs the dashboard + API. Works on Nebius Serverless Endpoints or any container host.
# Secrets (LLM_API_KEY, SARVAM_API_KEY) come from the host's environment settings, never the image.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tripowner ./tripowner
COPY dashboard ./dashboard
ENV PORT=8765
EXPOSE 8765
CMD ["sh", "-c", "uvicorn tripowner.server:app --host 0.0.0.0 --port ${PORT}"]
