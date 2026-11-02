# -*- coding: utf-8 -*-
"""
main.py —— Minecraft 服务器智能管家 · 程序入口
================================================================
职责：
1. 初始化控制台编码与日志格式（中文不乱码、日志带时间戳）；
2. 读取 config.json 并做一次启动前体检（占位符、缺字段都提前告诉你）；
3. 调用 logger.py 启动日志监听主循环；
4. 捕获 KeyboardInterrupt，保证 Ctrl+C 优雅退出。

运行：
    python main.py            # 正常启动
    python main.py -v         # 打开 DEBUG 级日志（能看到每一条日志原文）

说明：本文件不含任何个人化配置，所有可变项都在 config.json。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from tools import get_bot_name, get_permissions, get_trigger, load_config

LOG = logging.getLogger("main")

BANNER = r"""
  __  __  ____     ____  _______     _____   ___  ____
 |  \/  |/ ___|   / ___|| ____\ \   / / _ \ / _ \|  _ \
 | |\/| | |      \___ \|  _|  \ \ / / | | | | | | |_) |
 | |  | | |___    ___) | |___  \ V /| |_| | |_| |  _ <
 |_|  |_|\____|  |____/|_____|  \_/  \___/ \___/|_| \_\
 ---------------------------------------------------------------
 Minecraft 服务器 AI 管家 · 日志监听 → AI 决策 → RCON / SSH 执行
 ---------------------------------------------------------------
"""


# ================================================================ 一、基础设置
def _setup_console() -> None:
    """把标准输出切到 UTF-8，避免 Windows 控制台（cp936）打印中文时报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # reconfigure 在部分重定向场景下不可用，忽略即可，不影响功能
            pass


def _setup_logging(verbose: bool = False) -> None:
    """统一日志格式：时间戳 + 级别 + 模块名 + 内容。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 第三方库的 DEBUG 日志太吵，压到 WARNING
    for noisy in ("paramiko", "openai", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ================================================================ 二、配置体检
def _is_placeholder(value: Any) -> bool:
    """判断某个值是不是“还没填”的占位符。"""
    text = str(value or "")
    if not text:
        return True
    markers = ("在这里", "你的", "填入", "YOUR_", "your_", "CHANGE_ME", "changeme", "xxx")
    return any(marker in text for marker in markers)


def _mask(value: Any) -> str:
    """把密码类字段打码后再打印，避免日志里泄露凭据。"""
    text = str(value or "")
    if not text:
        return "(未填写)"
    if _is_placeholder(text):
        return "⚠ 还是占位符"
    if len(text) <= 4:
        return "****"
    return text[:2] + "*" * max(3, len(text) - 4) + text[-2:]


def _validate_config(cfg: Dict[str, Any]) -> List[str]:
    """
    启动前体检：把所有能提前发现的配置问题列出来（不直接退出，
    让你即便只改了一半也能先把程序跑起来看报错）。
    """
    problems: List[str] = []

    if _is_placeholder(cfg.get("deepseek_api_key")):
        problems.append("deepseek_api_key 还没填，AI 决策会失败（玩家会收到“大脑没接上线”）")

    ssh_cfg = cfg.get("ssh") or {}
    for field in ("host", "port", "username", "password", "remote_log_path"):
        if not ssh_cfg.get(field):
            problems.append(f"ssh.{field} 未配置")
    if _is_placeholder(ssh_cfg.get("password")):
        problems.append("ssh.password 还没填，日志监听会连接失败")

    rcon_cfg = cfg.get("rcon") or {}
    for field in ("host", "port", "password"):
        if not rcon_cfg.get(field):
            problems.append(f"rcon.{field} 未配置")
    if _is_placeholder(rcon_cfg.get("password")):
        problems.append("rcon.password 还没填，所有游戏内指令都会发送失败")

    perms = get_permissions(cfg)
    if not perms["op_whitelist"]:
        problems.append("permissions.op_whitelist 为空：目前没有任何玩家拥有管理权限")
    if not perms["absolute_block"]:
        problems.append("permissions.absolute_block 为空：黑名单失效，强烈建议补上")

    return problems


def _print_summary(cfg: Dict[str, Any]) -> None:
    """打印一张“脱敏版”启动摘要，方便一眼确认连的是哪台机器。"""
    ssh_cfg = cfg.get("ssh") or {}
    rcon_cfg = cfg.get("rcon") or {}
    perms = get_permissions(cfg)

    op_list = "、".join(perms["op_whitelist"]) or "（空）"
    allow_list = "、".join(perms["normal_player_allow"]) or "（空）"
    block_list = "、".join(perms["absolute_block"]) or "（空）"

    print("=" * 66)
    print("  启动配置摘要（敏感字段已打码）")
    print("-" * 66)
    print(f"  机器人名字     : {get_bot_name(cfg)}")
    print(f"  触发词         : {get_trigger(cfg)}")
    print(f"  AI 密钥        : {_mask(cfg.get('deepseek_api_key'))}")
    print(f"  AI 模型        : {cfg.get('deepseek_model') or '(默认)'}")
    print(f"  SSH            : {ssh_cfg.get('username')}@{ssh_cfg.get('host')}:{ssh_cfg.get('port')}"
          f"  密码={_mask(ssh_cfg.get('password'))}")
    print(f"  远端日志       : {ssh_cfg.get('remote_log_path')}")
    print(f"  RCON           : {rcon_cfg.get('host')}:{rcon_cfg.get('port')}"
          f"  密码={_mask(rcon_cfg.get('password'))}")
    print(f"  OP 白名单      : {op_list}")
    print(f"  玩家可用指令   : {allow_list}")
    print(f"  绝对禁止       : {block_list}")
    print("=" * 66)


# ================================================================ 三、主函数
def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = any(flag in argv for flag in ("-v", "--verbose", "-debug"))

    _setup_console()
    _setup_logging(verbose)
    print(BANNER)

    # ---- 读取配置 ----
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"[致命] 读取 config.json 失败：{exc}")
        print("       请检查文件是否存在、是否为合法 JSON（常见原因：多了个逗号、少了引号）。")
        return 2

    _print_summary(cfg)

    problems = _validate_config(cfg)
    if problems:
        print("\n[配置体检] 发现以下问题，建议先修正再使用：")
        for item in problems:
            print(f"   ⚠ {item}")
        print()
    else:
        print("\n[配置体检] 通过，凭据与权限矩阵都已就位。\n")

    # ---- 启动日志监听（惰性导入，避免 openai/paramiko 缺失时连摘要都看不到）----
    try:
        from logger import start_watch
    except Exception as exc:
        LOG.error("导入日志监听模块失败：%s", exc)
        print(f"[致命] 无法加载 logger.py：{exc}")
        print("       检查是否已安装依赖：pip install -r requirements.txt")
        return 3

    bot_name = get_bot_name(cfg)
    try:
        print(f"[启动] 正在连上远端主机监听服务器日志，等待玩家呼唤 {get_trigger(cfg)} ……")
        print("[提示] 按 Ctrl+C 可以优雅退出。\n")
        start_watch(cfg)
    except KeyboardInterrupt:
        print(f"\n[退出] 已收到 Ctrl+C，{bot_name}下班了，拜拜～")
        return 0
    except Exception:
        LOG.exception("主循环异常退出")
        print("\n[异常] 主循环意外退出，详细信息见上面的日志。")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
