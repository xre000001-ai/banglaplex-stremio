FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY addon.py ./
# Render injects its own PORT; 7055 is only the local default
ENV PORT=7055
EXPOSE 7055
CMD ["python3", "addon.py"]
