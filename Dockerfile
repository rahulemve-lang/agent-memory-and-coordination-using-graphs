FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir falkordb
COPY graphmem.py server.py ./
EXPOSE 8080
CMD ["python", "server.py"]
