FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PORT=7860
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Data is generated at build time so the image is self-contained and reproducible (fixed seed).
RUN python -m triage.generate_data && useradd -m -u 1000 app && chown -R app /app
USER app

EXPOSE 7860
CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0
