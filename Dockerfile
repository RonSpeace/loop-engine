# Loop Engine：看板 + 调度器单容器镜像（入口 scripts/run_local.sh，前台运行两个进程）
#
# 构建：docker build -t loop-engine .
# 运行：见 docker-compose.yml 与 README 的 Docker 一节。
#
# 安全：看板没有任何鉴权。镜像默认仍只监听 127.0.0.1（容器外不可达）；要从宿主机访问，
# 必须显式设置 REQ_BOARD_HOST=0.0.0.0，并且只把端口映射到宿主机回环地址（127.0.0.1:8899:8899）。

# Node.js（npx 拉起 claude-code-acp / codex-acp 等 ACP 适配器）取自官方镜像，避免 curl | bash 安装
FROM node:20-bookworm-slim AS node

FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates openssh-client curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# 非 root 运行；UID/GID 可在构建时对齐宿主机用户，避免挂载的仓库/数据目录权限问题
RUN groupadd --gid "${GID}" loop \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash loop

WORKDIR /app
COPY requirements.txt requirements-feishu.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# 可选：飞书通道依赖（docker build --build-arg WITH_FEISHU=1）
ARG WITH_FEISHU=0
RUN if [ "$WITH_FEISHU" = "1" ]; then pip install --no-cache-dir -r requirements-feishu.txt; fi

COPY --chown=loop:loop . /app

# 本地配置集中放 /config（目录挂载，原子替换写入才能成功）：
# - projects.json：看板经 REQ_BOARD_PROJECTS 读写；调度器固定读仓库根 projects.json，故用软链指过去
# - feishu.json：两个进程都认 REQ_BOARD_FEISHU_JSON
# 数据库在 /app/data（挂卷；REQ_BOARD_DATA 指向同一处）
RUN mkdir -p /config /app/data /home/loop/.req-board \
    && ln -sfn /config/projects.json /app/projects.json \
    && chown -R loop:loop /config /app/data /home/loop/.req-board

ENV PYTHONUNBUFFERED=1 \
    REQ_BOARD_PYTHON=python \
    REQ_BOARD_DATA=/app/data \
    REQ_BOARD_PROJECTS=/config/projects.json \
    REQ_BOARD_FEISHU_JSON=/config/feishu.json \
    REQ_BOARD_PORT=8899

USER loop
VOLUME ["/app/data", "/home/loop/.req-board"]
EXPOSE 8899

ENTRYPOINT ["/app/scripts/run_local.sh"]
CMD ["all"]
