FROM tensorflow/tensorflow:2.13.0

WORKDIR /app

# =========================
# 🔹 SYSTEM DEPENDENCIES
# =========================
RUN apt-get update && apt-get install -y --no-install-recommends \
    ansible \
    git \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Modern Docker CLI (Docker Engine 29+ rejects the old docker.io client)
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz \
    | tar xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker

# =========================
# 🔹 PYTHON DEPENDENCIES
# =========================
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
