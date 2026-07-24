FROM python:3.11-slim
WORKDIR /app

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

# agent/_sim_engine.py adds ../backend to sys.path at import time to reuse
# sim_v2.py's Kelly sizing directly — only sim_v2.py itself is needed here,
# not the FastAPI backend or its dependencies.
COPY backend/sim_v2.py backend/sim_v2.py
COPY agent/ agent/

CMD ["python", "-m", "agent.daemon"]
