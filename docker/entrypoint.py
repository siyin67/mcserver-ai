#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docker/entrypoint.py —— 容器启动器
================================================================
容器里要跑一个「配置写在 JSON 里」的程序，可 12-Factor 的部署方式是把配置写在
环境变量里。这个脚本负责在两者之间搭桥，并且额外承担健康检查：

    1. 生成配置 —— /app/config.json 不存在时，用环境变量 + config.example.json
       拼出一份真实配置；已经存在就直接用（尊重你手写的 / 挂载进来的配置）。
    2. 体检与探活 —— `check` 做一次启动前体检；`probe` 试连 RCON（ssh 模式再加
       SSH），给 Dockerfile 的 HEALTHCHECK 用。
    3. 启动主程序 —— 把 SIGTERM / SIGINT 转成 KeyboardInterrupt，
       这样 `docker stop` 也能走 main.py 里那条优雅退出分支。

用法：
    python docker/entrypoint.py            # 生成配置并启动管家
    python docker/entrypoint.py -v         # 参数原样透传给 main.py
    python docker/entrypoint.py check      # 只体检，不启动监听
    python docker/entrypoint.py probe      # 健康检查：连不上就返回 1

环境变量 → config.json 字段对照：

    环境变量                                    配置字段
    ------------------------------------------  ------------------------------
    MC_BOT_NAME                                 bot_name
    MC_TRIGGER_KEYWORD                          trigger_keyword
    MC_READ_MODE                                read_mode（local / ssh）
    MC_LOCAL_LOG_PATH                           local_log_path
    DEEPSEEK_API_KEY（或 MC_DEEPSEEK_API_KEY）   deepseek_api_key
    DEEPSEEK_BASE_URL                           deepseek_base_url
    DEEPSEEK_MODEL                              deepseek_model
    MC_SSH_HOST / MC_SSH_PORT                   ssh.host / ssh.port
    MC_SSH_USER / MC_SSH_PASSWORD               ssh.username / ssh.password
    MC_SSH_LOG_PATH                             ssh.remote_log_path
    MC_RCON_HOST / MC_RCON_PORT                 rcon.host / rcon.port
    MC_RCON_PASSWORD                            rcon.password
    MC_OP_WHITELIST                             permissions.op_whitelist
    MC_NORMAL_ALLOW                             permissions.normal_player_allow
    MC_ABSOLUTE_BLOCK                           permissions.absolute_block
    MC_RUNTIME_TEMPERATURE（任意 runtime 键）    runtime.temperature
    MC_CONFIG_PATH                              配置文件落盘位置（默认 /app/config.json）
    MC_FORCE_ENV_CONFIG=1                       即使已有 config.json 也按环境变量重写

列表类变量支持逗号、空格、分号、顿号分隔：MC_OP_WHITELIST=Steve,Alex
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List

# ================================================================ 一、路径
APP_DIR = Path(__file__).resolve().parent.parent          # 容器里就是 /app
EXAMPLE_PATH = APP_DIR / "config.example.json"
CONFIG_PATH = Path(
    os.environ.get("MC_CONFIG_PATH") or (APP_DIR / "config.json")
).expanduser()


def log(message: str) -> None:
    """带统一前缀打印，方便在 docker logs 里一眼分辨是不是启动器的输出。"""
    print(f"[entrypoint] {message}", flush=True)


# ================================================================ 二、环境变量读取
def env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量；全都没有就返回 default。"""
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    return default


def env_bool(*names: str, default: bool = False) -> bool:
    """把 1/true/yes/on/y/是 这些写法统一当成真。"""
    raw = env(*names)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y", "是")


def env_list(*names: str) -> List[str]:
    """
    把 "Steve,Alex" 或 "Steve Alex" 或 "Steve、Alex" 拆成列表。
    保持手写顺序并去重，空项丢掉。
    """
    raw = env(*names)
    if not raw:
        return []
    for separator in ("，", "、", ";", "|", "\n", "\t"):
        raw = raw.replace(separator, ",")
    items: List[str] = []
    for chunk in raw.split(","):
        for item in chunk.split():
            item = item.strip()
            if item and item not in items:
                items.append(item)
    return items


def _coerce(raw: str, reference: Any) -> Any:
    """按 example 里同一字段的类型把字符串转过去；转不动就原样返回，交给程序兜底。"""
    if isinstance(reference, bool):
        return raw.lower() in ("1", "true", "yes", "on", "y", "是")
    if isinstance(reference, int):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(reference, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _put(section: Dict[str, Any], key: str, value: str, cast: Any = None) -> None:
    """有值才写，避免用空环境变量把模板里的默认值抹掉。"""
    if not value:
        return
    if cast is int:
        try:
            section[key] = int(value)
            return
        except ValueError:
            return
    section[key] = value


# ================================================================ 三、生成配置
def build_config() -> Dict[str, Any]:
    """
    以 config.example.json 为底稿，把环境变量逐项覆盖上去。
    用模板打底的好处是：runtime、黑名单这些没在环境变量里出现的键保持默认值。
    """
    if EXAMPLE_PATH.exists():
        cfg: Dict[str, Any] = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    else:
        log(f"没找到模板 {EXAMPLE_PATH}，从空配置开始")
        cfg = {}

    # ---- 机器人身份 ----
    # 先记下模板里的值：只改了名字却没写触发词时，触发词应该跟着名字走
    template_bot = cfg.get("bot_name")
    template_trigger = cfg.get("trigger_keyword")
    bot_name = env("MC_BOT_NAME")
    if bot_name:
        cfg["bot_name"] = bot_name
    trigger = env("MC_TRIGGER_KEYWORD")
    if trigger:
        cfg["trigger_keyword"] = trigger
    elif bot_name and template_trigger in (None, "", f"@{template_bot}"):
        cfg["trigger_keyword"] = f"@{bot_name}"      # 只改名没改触发词时，跟着名字走

    # ---- 运行模式 ----
    mode = env("MC_READ_MODE")
    if mode:
        cfg["read_mode"] = mode.lower()
    local_log = env("MC_LOCAL_LOG_PATH")
    if local_log:
        cfg["local_log_path"] = local_log

    # ---- 大模型 ----
    for env_names, key in (
        (("DEEPSEEK_API_KEY", "MC_DEEPSEEK_API_KEY"), "deepseek_api_key"),
        (("DEEPSEEK_BASE_URL", "MC_DEEPSEEK_BASE_URL"), "deepseek_base_url"),
        (("DEEPSEEK_MODEL", "MC_DEEPSEEK_MODEL"), "deepseek_model"),
    ):
        value = env(*env_names)
        if value:
            cfg[key] = value

    # ---- SSH（只有 ssh 模式才用得上，local 模式填不填都行）----
    ssh = cfg.setdefault("ssh", {})
    _put(ssh, "host", env("MC_SSH_HOST"))
    _put(ssh, "port", env("MC_SSH_PORT"), int)
    _put(ssh, "username", env("MC_SSH_USER", "MC_SSH_USERNAME"))
    _put(ssh, "password", env("MC_SSH_PASSWORD"))
    _put(ssh, "remote_log_path", env("MC_SSH_LOG_PATH", "MC_SSH_REMOTE_LOG_PATH"))

    # ---- RCON ----
    rcon = cfg.setdefault("rcon", {})
    _put(rcon, "host", env("MC_RCON_HOST"))
    _put(rcon, "port", env("MC_RCON_PORT"), int)
    _put(rcon, "password", env("MC_RCON_PASSWORD"))

    # ---- 权限矩阵 ----
    perms = cfg.setdefault("permissions", {})
    for key, env_names in (
        ("op_whitelist", ("MC_OP_WHITELIST",)),
        ("normal_player_allow", ("MC_NORMAL_ALLOW",)),
        ("absolute_block", ("MC_ABSOLUTE_BLOCK",)),
    ):
        values = env_list(*env_names)
        if values:
            perms[key] = values

    # ---- runtime：MC_RUNTIME_<键名大写> 逐项覆盖 ----
    runtime = cfg.setdefault("runtime", {})
    for key, default in list(runtime.items()):
        raw = env(f"MC_RUNTIME_{key.upper()}")
        if raw:
            runtime[key] = _coerce(raw, default)

    return cfg


def write_config(cfg: Dict[str, Any]) -> None:
    """原子落盘 + 0600 权限：文件里有 API Key 和密码，不能让别人读。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, ensure_ascii=False, indent=4) + "\n"
    tmp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:                       # Windows 挂载卷上可能不支持，忽略
        pass
    tmp_path.replace(CONFIG_PATH)


def prepare_config() -> None:
    """有配置就用配置，没有才生成；想强制用环境变量就设 MC_FORCE_ENV_CONFIG=1。"""
    if CONFIG_PATH.exists() and not env_bool("MC_FORCE_ENV_CONFIG"):
        log(f"使用已存在的配置文件：{CONFIG_PATH}")
        log("（想让它按环境变量重新生成，把 MC_FORCE_ENV_CONFIG 设为 1）")
        return
    write_config(build_config())
    log(f"已根据环境变量生成配置：{CONFIG_PATH}")


def effective_config() -> Dict[str, Any]:
    """体检 / 探活时用：优先读磁盘上的配置，读不到就现拼一份（不落盘）。"""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"读取 {CONFIG_PATH} 失败（{exc}），改用环境变量")
    return build_config()


# ================================================================ 四、体检与探活
def check() -> int:
    """只做启动前体检，不进入监听循环。适合 `docker compose run --rm mc-agent check`。"""
    prepare_config()
    sys.path.insert(0, str(APP_DIR))
    from main import _print_summary, _validate_config   # 延迟导入：probe 不该拉起这些
    from tools import load_config

    cfg = load_config()
    _print_summary(cfg)
    problems = _validate_config(cfg)
    if problems:
        print("\n[体检] 发现以下问题：")
        for item in problems:
            print(f"   ⚠ {item}")
        return 1
    print("\n[体检] 通过。")
    return 0


def probe() -> int:
    """
    健康检查：只测 TCP 能不能连上，不发任何业务请求（不花 RCON 连接数、不打扰服务器）。
    ssh 模式下 RCON 和 SSH 都得通；local 模式只测 RCON。
    """
    cfg = effective_config()
    targets: List[Any] = [("RCON", cfg.get("rcon") or {})]
    mode = str(cfg.get("read_mode") or "ssh").strip().lower()
    if mode not in ("local", "本地", "本机"):
        targets.append(("SSH", cfg.get("ssh") or {}))

    try:
        timeout = float(env("MC_PROBE_TIMEOUT", default="3") or 3)
    except ValueError:
        timeout = 3.0

    ok = True
    for name, section in targets:
        host = str(section.get("host") or "")
        port = section.get("port")
        if not host or not port:
            log(f"{name}: 未配置 host/port，跳过")
            continue
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                log(f"{name}: {host}:{port} 可连通")
        except (OSError, ValueError) as exc:
            ok = False
            log(f"{name}: {host}:{port} 连不上（{type(exc).__name__}: {exc}）")
    return 0 if ok else 1


# ================================================================ 五、启动主程序
def install_signal_handlers() -> None:
    """
    把 SIGTERM / SIGINT 转成 KeyboardInterrupt。
    Docker 的 `docker stop` 发的是 SIGTERM，而 main.py 只在 KeyboardInterrupt 上做优雅退出，
    所以这里帮它把信号翻译过去。
    """
    def _graceful(signum, frame):       # noqa: ANN001 - signal handler 签名固定
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:                 # Windows 上没有 SIGTERM 的部分变体
            continue
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):   # 非主线程 / 受限环境，忽略即可
            pass


def run_agent(argv: List[str]) -> int:
    """生成配置 → 切到 /app → 交给 main.main() 跑监听循环。"""
    prepare_config()
    os.chdir(APP_DIR)
    sys.path.insert(0, str(APP_DIR))
    install_signal_handlers()
    from main import main as agent_main
    return agent_main(argv)


def main(argv: List[str]) -> int:
    if argv and argv[0] == "probe":
        return probe()
    if argv and argv[0] == "check":
        return check()
    try:
        return run_agent(argv)
    except KeyboardInterrupt:
        print("\n[entrypoint] 收到终止信号，已优雅退出。", flush=True)
        return 0
    except Exception as exc:            # 启动阶段的任何异常都别留一个裸 traceback
        print(f"[entrypoint] 启动失败：{type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
