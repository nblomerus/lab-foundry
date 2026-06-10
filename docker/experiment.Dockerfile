# Reproducible base image for the lab's sandboxed code experiments.
#
# Built once (`make experiment-image`) and pinned by tag/digest so every
# experiment runs in the identical environment — the digest is recorded into
# experiment_runs.provenance for reproducibility. CUDA-enabled so GPU experiments
# (small-model fine-tune / inference / benchmarks) run within the lab envelope
# (single modest GPU, <=~32B). The Quartermaster runs containers from this image
# with --network none, read-only rootfs, a tmpfs /work, mem/cpu/pids caps, and
# (for GPU runs) --gpus device=<n>; this image just provides the toolchain.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.11 python3-pip python3.11-venv \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && rm -rf /var/lib/apt/lists/*

# The scientific stack, version-pinned for reproducibility. torch is the CUDA
# build (cu124 wheels) so experiments can use the GPU; the rest is CPU science.
RUN python -m pip install --upgrade pip==24.2 \
    && python -m pip install \
        numpy==2.1.3 \
        scipy==1.14.1 \
        pandas==2.2.3 \
        scikit-learn==1.5.2 \
        xgboost==2.1.2 \
        statsmodels==0.14.4 \
        torch==2.5.1

# Non-root: experiments never need privileges; combined with --cap-drop ALL and
# --security-opt no-new-privileges this is the in-container least-privilege floor.
RUN useradd --create-home --uid 10001 experiment
USER experiment
WORKDIR /work

# The Quartermaster overrides CMD with `python /work/exp.py`; this default just
# documents the contract (the script prints one JSON object as its result).
CMD ["python", "-c", "print('{\"error\": \"no experiment script mounted\"}')"]
