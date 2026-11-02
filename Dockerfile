# syntax=docker/dockerfile:1
# ================================================================
# mcserver-ai · Minecraft 服务器 AI 管家
# ================================================================
# 构建：docker build -t mcserver-ai .
# 运行：docker run -d --name mc-agent --env-file .env mcserver-ai
# 或者直接用 docker-compose.yml：docker compose up -d --build
#
# 说明：镜像里不烘焙任何配置，config.json 由 docker/entrypoint.py 在启动时
#       根据环境变量生成（或使用你挂载进来的 /app/config.json）。
# ================================================================

FROM python:3.12-slim

# ---- 运行参数 ----
# PYTHONUNBUFFERED 让日志实时进 docker logs，不然 24/7 跑着看不到输出
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    MC_CONFIG_PATH=/app/config.json

# 非 root 运行；UID/GID 可在构建时改，好和宿主机上日志文件的属主对齐
ARG UID=1000
ARG GID=1000

# ---- 构建加速（可选，默认官方源）----
# 国内网络慢就传（或在 .env 里设同名变量，compose 会自动带进来）：
#   APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 用 ARG 而不是写死镜像源：别人构建不受影响，你也不用改这个文件。
ARG APT_MIRROR=
ARG PIP_INDEX_URL=

WORKDIR /app

# tzdata 只为了让日志时间戳是你所在时区，不是 UTC
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null \
        || sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata; \
    rm -rf /var/lib/apt/lists/*

# ---- 依赖单独一层，改代码不用重装依赖 ----
COPY requirements.txt ./
RUN set -eux; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
        pip install --no-cache-dir -r requirements.txt -i "${PIP_INDEX_URL}"; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# ---- 非 root 用户 ----
RUN groupadd --gid "${GID}" mcagent \
 && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin mcagent \
 && chown mcagent:mcagent /app

# ---- 数据目录：卷挂到这里，长期记忆（memory.json）就跨容器重建保留 ----
# 用命名卷时 Docker 会把镜像里 /data 的属主一起带过去，因此容器内开箱即可写；
# 用宿主机目录挂载（bind）时属主以宿主机为准，需要自己 chown 成上面那个 UID。
RUN mkdir -p /data && chown mcagent:mcagent /data

# ---- 代码（显式列举，避免把 config.json / .env 之类的秘密带进镜像）----
COPY --chown=mcagent:mcagent main.py agent_core.py logger.py tools.py config.example.json ./
COPY --chown=mcagent:mcagent docker/ ./docker/

USER mcagent

# 健康检查：只做 TCP 探活，不发业务请求
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/entrypoint.py", "probe"]

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD []
