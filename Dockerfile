FROM python:3.10-slim

# Install FFmpeg and system tools
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 && apt-get clean

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]

