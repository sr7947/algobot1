FROM python:3.11-slim

WORKDIR /app

# Install system dependencies + Node.js for Vite Dashboard build
RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    g++ \
    libpq-dev \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip & build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Build Vite React Dashboard
RUN cd dashboard && npm install && npm run build

# Create runtime directories
RUN mkdir -p logs data backtests

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
