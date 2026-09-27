FROM python:3.11-slim

WORKDIR /app

COPY index.html server.py ./

ENV PORT=8899
EXPOSE 8899

CMD ["python", "server.py", "--host", "0.0.0.0", "--no-open"]
