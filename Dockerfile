FROM python:3.13-slim

# Install LaTeX and clean up in one layer to reduce image size
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-lang-german \
    texlive-plain-generic \
    texlive-fonts-recommended && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies directly with pip — no Poetry needed at runtime
RUN pip install --no-cache-dir \
    "imapclient>=3.1.0,<4.0.0" \
    "pyzmail36>=1.0.5,<2.0.0" \
    "python-dotenv>=1.0.0" \
    "beautifulsoup4>=4.14.3,<5.0.0" \
    "flask>=3.0.0,<4.0.0"

# Copy application code
COPY src/latex_email_daemon/*.py ./src/latex_email_daemon/

# Create necessary directories
RUN mkdir -p src/latex_email_daemon/emails \
             src/latex_email_daemon/pdfs \
             src/latex_email_daemon/data \
             src/latex_email_daemon/templates

WORKDIR /app/src/latex_email_daemon

# Expose web dashboard port (used when running web.py)
EXPOSE 5000

# Default: run the email daemon.
# Override with `command: ["python", "web.py"]` in docker-compose to start
# the web front-end instead (or alongside the daemon as a separate service).
CMD ["python", "main.py"]
