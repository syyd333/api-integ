#!/usr/bin/env bash
# Starts Docker Desktop, waits for the engine, then runs the Hindsight
# container detached. Reads OPENAI_API_KEY from files/.env (same as app.py).
#
# Usage:  bash start_hindsight.sh
set -euo pipefail

cd "$(dirname "$0")"

# --- load OPENAI_API_KEY from .env ---
if [ ! -f .env ]; then
  echo "ERROR: files/.env not found."
  echo "Copy .env.example to .env and add your keys first, then re-run this script."
  exit 1
fi
OPENAI_API_KEY=$(grep -E '^OPENAI_API_KEY=' .env | cut -d= -f2- | tr -d '"' || true)
if [ -z "${OPENAI_API_KEY}" ]; then
  echo "ERROR: OPENAI_API_KEY missing in files/.env (Hindsight needs it to extract memories)."
  exit 1
fi

# --- start Docker Desktop if the engine is not running ---
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop..."
  "/c/Program Files/Docker/Docker/Docker Desktop.exe" &
  for i in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then break; fi
    sleep 2
  done
  docker info >/dev/null 2>&1 || { echo "ERROR: Docker engine did not start."; exit 1; }
fi
echo "Docker engine is up."

# --- run Hindsight (detached, survives terminal close) ---
echo "Starting Hindsight container on :8888..."
docker run --rm -d --pull always --name hindsight \
  -p 8888:8888 -p 9999:9999 \
  -e HINDSIGHT_API_LLM_API_KEY="${OPENAI_API_KEY}" \
  -v "$HOME/.hindsight-docker:/home/hindsight/.pg0" \
  ghcr.io/vectorize-io/hindsight:latest

# --- wait for the API to answer ---
echo -n "Waiting for Hindsight API"
for i in $(seq 1 45); do
  if curl -s -o /dev/null http://localhost:8888; then echo " - up!"; exit 0; fi
  echo -n "."
  sleep 2
done
echo " - container started but API not answering yet; check: docker logs hindsight"
