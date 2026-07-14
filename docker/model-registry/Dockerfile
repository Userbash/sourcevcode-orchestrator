FROM python:3.11-alpine
WORKDIR /app
COPY docker/model-registry/app.py /app/app.py
EXPOSE 8090
ENTRYPOINT ["python", "/app/app.py"]
