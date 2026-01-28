FROM python:3.13-slim

# Install LaTeX and clean up in one layer to reduce image size
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-extra && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Install poetry and dependencies
RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi

# Copy application code (excluding template.tex - that goes in a volume)
COPY src/latex_email_daemon/*.py ./src/latex_email_daemon/
COPY src/latex_email_daemon/__init__.py ./src/latex_email_daemon/

# Create necessary directories
RUN mkdir -p src/latex_email_daemon/emails src/latex_email_daemon/pdfs src/latex_email_daemon/data src/latex_email_daemon/templates

WORKDIR /app/src/latex_email_daemon

# Run the main script
CMD ["python", "main.py"]
