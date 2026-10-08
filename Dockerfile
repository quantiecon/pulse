FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PULSE_DATA_DIR=/data
EXPOSE 8787
CMD ["pulse", "serve", "--host", "0.0.0.0", "--port", "8787"]
