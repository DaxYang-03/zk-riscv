# syntax=docker/dockerfile:1
FROM rust:1.88.0-slim-bookworm AS proof-build
WORKDIR /src
COPY proof/Cargo.toml proof/Cargo.lock ./proof/
COPY proof/src/ ./proof/src/
RUN cargo build --locked --release --manifest-path proof/Cargo.toml

FROM python:3.13-slim-bookworm AS service-build
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends iverilog \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.13.5
COPY rtl/ ./rtl/
RUN mkdir /artifacts \
    && iverilog -g2012 -Wall -s tb_trace -o /artifacts/tb_trace.vvp \
        rtl/zk_rv32i_addi.sv rtl/tb_trace.sv
ENV UV_PROJECT_ENVIRONMENT=/opt/zk-venv UV_PYTHON_DOWNLOADS=never
COPY service/ ./service/
RUN uv sync --locked --no-dev --no-editable --project service

FROM python:3.13-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends iverilog \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 zk \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin zk \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/zk-riscv
WORKDIR /opt/zk
COPY LICENSE ./
COPY --from=service-build /opt/zk-venv /opt/zk-venv
COPY --from=service-build /artifacts/tb_trace.vvp /opt/zk/tb_trace.vvp
COPY --from=proof-build /src/proof/target/release/zk-riscv-proof /opt/zk/zk-riscv-proof
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ZKR_HOST=0.0.0.0 \
    ZKR_PORT=8080 \
    ZKR_DB_PATH=/var/lib/zk-riscv/jobs.sqlite3 \
    ZKR_VVP=/usr/bin/vvp \
    ZKR_SIMULATOR=/opt/zk/tb_trace.vvp \
    ZKR_PROVER=/opt/zk/zk-riscv-proof
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
    CMD ["/opt/zk-venv/bin/python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['ZKR_PORT'] + '/readyz', timeout=2).close()"]
CMD ["/opt/zk-venv/bin/python", "-m", "zkservice"]
