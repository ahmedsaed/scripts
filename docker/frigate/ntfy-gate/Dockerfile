FROM python:3.12-alpine
RUN pip install --no-cache-dir paho-mqtt==2.1.0
# Code is mounted at /app by docker-compose, so edits only need a restart
WORKDIR /app
CMD ["python", "-u", "gate.py"]
