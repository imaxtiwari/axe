FROM ghcr.io/astral-sh/uv@sha256:0437fac418c701b4fbbfa3afce633fb439a97631bb74a912325682d2de59f082 AS uv
FROM sha256:b9ed0374e71a2f564e656ddefbb4fe324e08e11c2517873eecee810270144cd5
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /inputs/
COPY validation/step03b/export_requirements.py /workspace/export_requirements.py
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/scratch", "TMPDIR=/scratch", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "UV_CACHE_DIR=/scratch/uv-cache", "UV_PYTHON_DOWNLOADS=never", "UV_OFFLINE=1", "/usr/local/bin/python", "-I", "-S", "-B", "-u", "/workspace/export_requirements.py"]
