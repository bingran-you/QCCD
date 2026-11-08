# The reviewer image of a Docker review (qccd/review/container.py): the two agent CLIs at pinned
# versions, Python with numpy for the design-review skill's script, and the egress proxy.  Built on
# the person's own computer the first time a review asks for Docker isolation; its tag is the digest
# of this folder, so a change here is a new image.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.312
ARG CODEX_VERSION=0.166.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-numpy ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

COPY egress_proxy.py /opt/qccd/egress_proxy.py

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /job
