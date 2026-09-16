FROM docker.io/library/python@sha256:d04f49f5882f49a3b91f874e75e19f0c265f7222da8659741a9d7eab148f22a9

RUN mkdir -p /workspace /scratch \
    && printf 'synthetic-source-canary\n' > /workspace/canary \
    && printf 'synthetic-secret-only\n' > /workspace/secret \
    && chmod 0555 /workspace \
    && chmod 0666 /workspace/canary \
    && chmod 0400 /workspace/secret \
    && chown 10001:10001 /scratch \
    && chmod 0700 /scratch \
    && chmod 1777 /tmp

COPY selftest.py /workspace/selftest.py
RUN chmod 0444 /workspace/selftest.py

USER 10001:10001
WORKDIR /scratch
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/scratch", "TMPDIR=/scratch", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "/usr/local/bin/python", "-I", "-S", "-B", "-u", "/workspace/selftest.py"]
