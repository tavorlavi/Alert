FROM python:3.12-slim

# Set environment variables
ENV PORT=7860
ENV PYTHONUNBUFFERED=1

# Create a non-root user for Hugging Face Spaces (UID 1000)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Copy requirements and install
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy the rest of the application
COPY --chown=user . .

EXPOSE 7860

CMD ["python", "server.py"]

