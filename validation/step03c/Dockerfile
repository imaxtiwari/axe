FROM docker.io/library/node@sha256:8d342e46d3b2883df69f797cb60fc71d8a0b65de65ddfbf4bf63fdc02049615f AS node
FROM sha256:b9ed0374e71a2f564e656ddefbb4fe324e08e11c2517873eecee810270144cd5
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY validation/step03b/installed-18fb41f9-526b-4423-a7b1-c64b99759f0c/dependencies/ /usr/local/lib/python3.12/site-packages/
COPY src/ /app/src/
COPY tests/ /app/tests/
COPY pyproject.toml /app/pyproject.toml
COPY scripts/ /app/scripts/
COPY validation/step03c/runner.py validation/step03c/test_settings.py /harness/
ENTRYPOINT ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/scratch", "TMPDIR=/scratch", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "/usr/local/bin/python", "-I", "-S", "-B", "-u", "/harness/runner.py"]
