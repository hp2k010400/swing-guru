# Swing Guru analysis service: FastAPI API + in-process worker running the unchanged engine.
# Build:  docker build -t swingguru-service .
# Run:    docker run --env-file .env -p 8790:8790 swingguru-service
FROM python:3.12-slim-bookworm

# Fixed thread pools. The container sees the host's cores (48 on Railway) but its quota is 8 CPUs;
# left unset, ONNX Runtime and OpenCV size their pools from the host count and oversubscribe the
# quota (pose ran 5x slower). 6 is also the largest pose thread count whose keypoints are
# bit-identical to the reference run (8 changes float reduction order). See docs/deployment/PRODUCTION.md.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    XDG_CACHE_HOME=/opt/models/cache \
    HF_HOME=/opt/models/hf \
    MPLCONFIGDIR=/tmp/matplotlib \
    SG_ORT_THREADS=6 \
    OMP_NUM_THREADS=6 \
    OPENCV_FOR_THREADS_NUM=6

# ffmpeg: the browser-playable replay video. GL/GLib: OpenCV + MediaPipe runtime libraries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 libegl1 libgles2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements/service.txt requirements/service.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0 torchvision==0.29.0 \
 && pip install --no-deps rtmlib==0.0.16 \
 && pip install -r requirements/service.txt

# Models are part of the image: no downloads at request time. This layer only
# depends on the fetch script, so code changes reuse it from the build cache.
COPY scripts/fetch_models.py scripts/fetch_models.py
RUN python scripts/fetch_models.py /app/models \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/var && chown -R app:app /app/var /opt/models

COPY src/ src/
COPY scripts/build_analysis.py scripts/

# The container starts as root only so container_start.sh can hand the data directories (the Railway volume,
# mounted root-owned) to `app`; it then drops to `app` (setpriv) and execs the service. The service never runs as root.
COPY scripts/container_start.sh /usr/local/bin/container_start
RUN sed -i 's/\r$//' /usr/local/bin/container_start && chmod 0755 /usr/local/bin/container_start \
 && setpriv --version && id app

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    SG_DATA_DIR=/app/var/service \
    PORT=8790
EXPOSE 8790
ENTRYPOINT ["/usr/local/bin/container_start"]
CMD ["python", "-m", "swingguru.service"]
