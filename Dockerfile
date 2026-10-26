# UBEM-MCP, ready to run: EnergyPlus 26.1 and the ubem-mcp package.
#
#   docker run --rm -i -v "$PWD":/data ghcr.io/tensorbuildlab/ubem-mcp:1.0.0          # MCP server over stdio
#   docker run --rm -v "$PWD":/data ghcr.io/tensorbuildlab/ubem-mcp:1.0.0 tools      # command line
#
# Files in the mounted folder (/data) are the workspace: inputs are read from
# it and outputs are written to /data/outputs. For development, use the image
# in .devcontainer/ instead, which mounts the source tree.
FROM python:3.12-slim

ARG TARGETPLATFORM
RUN apt-get update && \
    apt-get install -y --no-install-recommends wget ca-certificates graphviz libgomp1 \
        libx11-6 libxext6 libxrender1 libxtst6 libxi6 libxrandr2 libxinerama1 libxcursor1 && \
    rm -rf /var/lib/apt/lists/*

# Keep in sync with .devcontainer/Dockerfile and .github/workflows/ci.yml.
ARG EPLUS_VER=26.1.0
ARG EPLUS_HASH=6f2e40d102
ARG EPLUS_PREFIX=/app/software/EnergyPlusV26-1-0
ARG EPLUS_DIST_SUFFIX=Ubuntu24.04
RUN set -eux; \
    case "${TARGETPLATFORM:-linux/amd64}" in \
      linux/arm64) dist="Linux-${EPLUS_DIST_SUFFIX}-arm64" ;; \
      *) dist="Linux-${EPLUS_DIST_SUFFIX}-x86_64" ;; \
    esac; \
    mkdir -p "${EPLUS_PREFIX}"; \
    wget -q "https://github.com/NREL/EnergyPlus/releases/download/v${EPLUS_VER}/EnergyPlus-${EPLUS_VER}-${EPLUS_HASH}-${dist}.tar.gz" -O /tmp/eplus.tgz; \
    tar -xzf /tmp/eplus.tgz -C "${EPLUS_PREFIX}" --strip-components=1; \
    rm /tmp/eplus.tgz

ENV EPLUS_IDD_PATH=${EPLUS_PREFIX}/Energy+.idd \
    PATH=${EPLUS_PREFIX}:${PATH} \
    UBEM_WORKSPACE_ROOT=/data \
    PYTHONUNBUFFERED=1

COPY src /opt/ubem-mcp-server
RUN pip install --no-cache-dir /opt/ubem-mcp-server && rm -rf /opt/ubem-mcp-server/tests

WORKDIR /data
ENTRYPOINT ["ubem-mcp"]
CMD ["serve"]
