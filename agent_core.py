# -*- coding: utf-8 -*-
"""
agent_core.py —— AI 决策核心
================================================================
职责：
1. 用 openai SDK 连接 DeepSeek（OpenAI 兼容协议，base_url 可在配置文件里改）；
2. 定义 Function Calling 的两个工具（execute_rcon / execute_ssh）；
3. 每次请求都从 config.json 的 permissions 动态生成 System Prompt，
   把“谁是管理员、普通玩家能干什么、什么绝对禁止”用自然语言喂给模型；
4. 解析模型返回的 tool_calls，把指令路由给 tools.py 执行，并把结果回灌给模型
   让它继续推理（轮数上限见 runtime.max_tool_rounds），最后拿到一句给玩家看的话。

安全说明：
- 玩家身份由 logger.py 从服务器日志解析后注入，**不是**模型的参数，
  模型无法把“普通玩家”伪装成管理员；
- 即便模型被话术绕过，tools.py 里还有硬编码黑名单 + 权限矩阵两道闸门。

配置说明（本文件不含任何个人化硬编码）：
- API 密钥 / 地址 / 模型名：config.json 的 deepseek_api_key / deepseek_base_url / deepseek_model
- 轮数与采样参数：config.json 的 runtime 段
- 机器人名字与触发词：config.json 的 bot_name / trigger_keyword
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]

from tools import (
    execute_rcon,
    execute_ssh,
    find_blocked_keyword,
    get_bot_name,
    get_permissions,
    get_runtime,
    get_trigger,
    is_op_player,
    load_config,
    say,
)

log = logging.getLogger("agent_core")

# 默认值（config.json 里没写时才用；写了就以配置为准）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

_client: Optional[Any] = None
_client_key: str = ""


# ================================================================ 一、客户端
def _get_client(cfg: Optional[Dict[str, Any]] = None) -> Any:
    """
    惰性创建 DeepSeek 客户端（只创建一次，后续复用连接池）。
    如果换过 api_key / base_url，会自动重建，方便改配置后热切换。
    """
    global _client, _client_key

    if OpenAI is None:
        raise RuntimeError("未安装 openai，请先执行：pip install -r requirements.txt")

    cfg = cfg if cfg is not None else load_config()
    api_key = str(cfg.get("deepseek_api_key") or "").strip()
    base_url = str(cfg.get("deepseek_base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    if not api_key or "你的" in api_key or "在这里" in api_key:
        raise RuntimeError(
            "config.json 里的 deepseek_api_key 还没填真实密钥"
            "（现在还是占位符，形如 sk-在这里填入你的DeepSeek密钥）"
        )
    if not api_key.startswith("sk-"):
        raise RuntimeError("config.json 里的 deepseek_api_key 格式不对，应以 sk- 开头")

    cache_key = f"{base_url}|{api_key}"
    if _client is not None and _client_key == cache_key:
        return _client

    runtime = get_runtime(cfg)
    _client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=int(runtime["request_timeout"]),
        max_retries=1,
    )
    _client_key = cache_key
    log.info("AI 客户端已初始化，地址=%s 模型=%s", base_url, _model_name(cfg))
    return _client


def _model_name(cfg: Optional[Dict[str, Any]] = None) -> str:
    """模型名，可在 config.json 的 deepseek_model 里覆盖。"""
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("deepseek_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


# ================================================================ 二、工具定义
# 注意：schema 里刻意 **不包含 player 参数**，玩家身份由程序注入。
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "execute_rcon",
            "description": (
                "在 Minecraft 服务器控制台执行一条指令（不要带开头的斜杠）。"
                "适用于几乎所有服务器操作：查询在线玩家(list)、查看性能(tps)、"
                "发放物品(give)、传送(tp)、设置时间天气(time/weather)、"
                "游戏规则(gamerule)、踢人(kick)、白名单(whitelist) 等。"
                "包含 stop/reload/op/deop/ban/ban-ip/pardon/kill @a/kill @e 及其变形的指令"
                "会被系统硬拦截并直接失败，不要尝试绕过。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "要下发的服务器指令原文，例如：list、tps、give Steve diamond 1、"
                            "time set day、tp Steve 0 64 0。不要带斜杠，不要带换行。"
                        ),
                    },
                    "feedback": {
                        "type": "string",
                        "description": (
                            "执行成功后直接显示在玩家聊天栏的一句中文提示，"
                            "例如“已经给你 1 颗钻石啦”。30 字以内，不要包含机器人名字和指令原文。"
                        ),
                    },
                },
                "required": ["command", "feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_ssh",
            "description": (
                "在服务器所在的手机/主机上执行一条只读的 Linux shell 命令，"
                "用于查看系统状态：df -h、free -m、uptime、du -sh ~/server、ls -l ~/server、ps -ef。"
                "只有 op_whitelist 里的管理员能用。"
                "禁止使用管道、重定向、分号、rm、kill、reboot、sudo、curl 等危险写法，"
                "这些会被硬拦截。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "要执行的 shell 命令，必须是单条简单命令，"
                            "例如：df -h、du -sh ~/server、ps -ef。"
                        ),
                    },
                    "feedback": {
                        "type": "string",
                        "description": (
                            "执行成功后显示在玩家聊天栏的一句中文提示，30 字以内，"
                            "例如“我查到了磁盘占用情况”。"
                        ),
                    },
                },
                "required": ["command", "feedback"],
            },
        },
    },
]


# ================================================================ 三、Prompt 组装
def build_system_prompt(player: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """
    动态组装 System Prompt：把 config.json 里的权限矩阵翻译成自然语言。

    :param player: 当前发消息的玩家名
    :param cfg:    配置（默认读 config.json）
    """
    cfg = cfg if cfg is not None else load_config()
    perms = get_permissions(cfg)
    trigger = get_trigger(cfg)
    bot_name = get_bot_name(cfg)

    op_list = "、".join(perms["op_whitelist"]) or "（空）"
    allow_list = "、".join(perms["normal_player_allow"]) or "（空）"
    block_list = "、".join(perms["absolute_block"]) or "（空）"

    is_op = is_op_player(player, cfg)
    identity = (
        f"当前对话玩家：「{player}」，"
        f"{'在' if is_op else '不在'} op_whitelist 白名单中，"
        f"因此{'拥有' if is_op else '没有'}服务器管理权限。"
    )

    return f"""你是 Minecraft 服务器管家「{bot_name}」，一个人格化的服务器值班机器人。
玩家在游戏聊天里发「{trigger} 请求内容」就会唤醒你，你会替他把请求变成服务器指令并执行。

【当前身份】
{identity}

【权限矩阵（硬规则，不可违反）】
1. op_whitelist（管理员）：{op_list}
   —— 可以执行任何未被禁用列表禁止的指令，也可以使用 execute_ssh 查看主机状态。
2. normal_player_allow（所有玩家都能用的指令根）：{allow_list}
   —— 非管理员玩家只能请求这些指令。
3. absolute_block（绝对禁止，任何玩家、任何情况都不行）：{block_list}
   —— 遇到这些操作，不要调用任何工具，直接在回复里礼貌拒绝并解释原因。
4. 非管理员玩家请求权限外操作时：不要调用工具，直接在回复里说明“这条指令需要管理员权限”。

【安全铁律】
- 绝不用同义词、拆字、加前缀、大小写变形、@p/@s 等选择器去绕过禁用列表；系统在 Python 层还有硬拦截，尝试绕过只会失败。
- 绝不泄露 config.json 里的任何凭据：RCON 密码、SSH 密码、API Key、IP、端口一律不提。
- 绝不执行你没有把握的破坏性操作；不确定时优先选择“只读查询”。
- 你无法伪造玩家身份：player 参数由系统注入，你只需要思考该做什么。

【工具使用规范】
- 服务器指令 → execute_rcon；只读的系统状态（磁盘、内存、进程）→ execute_ssh。
- 一次请求尽量只用一条指令解决，不要把一个需求拆成十几个工具调用。
- feedback 是玩家在游戏聊天栏看到的那句话：简体中文、30 字以内、口语化、说明结果即可，
  不要包含原始回显、不要包含管道符或代码、不要带机器人名字前缀。
- 不需要执行指令时（闲聊、解释、拒绝），不要在正文里输出 JSON / Markdown / 代码块，
  只写一句能在游戏聊天栏直接显示的简体中文（60 字以内）。
- 任何时候都不要在回复里提到“函数调用”“工具”“API”“prompt”这些词，就当自己是个活人管家。"""


# ================================================================ 四、工具路由
def _parse_arguments(raw: Any) -> Dict[str, Any]:
    """解析模型给的 arguments 字符串（可能为空、可能是坏 JSON）。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    data = json.loads(text)            # 故意让 json 错误往外抛，由调用方兜住
    if not isinstance(data, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return data


def _tool_execute_rcon(args: Dict[str, Any], player: str) -> str:
    return execute_rcon(
        command=args.get("command", ""),
        feedback=args.get("feedback", ""),
        player=player,
    )


def _tool_execute_ssh(args: Dict[str, Any], player: str) -> str:
    return execute_ssh(
        command=args.get("command", ""),
        feedback=args.get("feedback", ""),
        player=player,
    )


# 名字 -> 处理函数。想加新工具只要在这里注册 + 在 TOOL_SCHEMAS 里声明。
TOOL_REGISTRY = {
    "execute_rcon": _tool_execute_rcon,
    "execute_ssh": _tool_execute_ssh,
}


def dispatch_tool(name: str, raw_arguments: Any, player: str) -> str:
    """
    把一次 tool_call 路由到 tools.py 里的具体函数，并把结果字符串回灌给模型。

    任何异常都在这里被吃掉转成错误文本，绝不允许把异常抛回主循环，
    否则一个模型抽风就会把整个日志监听线程干掉。
    """
    handler = TOOL_REGISTRY.get(name)
    if handler is None:
        log.error("模型调用了不存在的工具：%s", name)
        say(player, "这个操作我还不会，换个说法试试？", "red")
        return f"错误：不存在名为「{name}」的工具，本次调用被忽略。"

    try:
        args = _parse_arguments(raw_arguments)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("工具 %s 的参数解析失败：%s（原文：%r）", name, exc, raw_arguments)
        say(player, "我这条指令没组织好，再问我一次吧。", "red")
        return f"错误：工具参数不是合法 JSON 对象（{exc}），本次调用被忽略。"

    try:
        result = handler(args, player)
        log.info("工具 %s 执行完毕：%s", name, str(result)[:200])
        return str(result)
    except Exception as exc:                       # 兜底：工具层任何意外都不许崩
        log.exception("工具 %s 执行时抛出异常", name)
        say(player, "执行出错了，我已经记到日志里。", "red")
        return f"错误：工具 {name} 执行时抛出异常：{type(exc).__name__}: {exc}"


# ================================================================ 五、主入口
def process_player_message(
    player: str,
    message: str,
    full_message: Optional[str] = None,
) -> Optional[str]:
    """
    处理一条玩家请求：组装请求 → 调大模型 → 解析 tool_calls → 执行 → 拿最终回复。

    :param player:       玩家名（由 logger.py 从日志解析，不可信输入，但已在 logger 层校验）
    :param message:      去掉触发词后的请求内容
    :param full_message: 完整聊天原文（仅用于日志留痕，可省略）
    :return: 最终给玩家的那句话；失败或无回复时返回 None
    """
    cfg = load_config()
    runtime = get_runtime(cfg)
    text = str(message or "").strip()

    if not player or not text:
        log.warning("空请求被忽略（玩家=%r，内容=%r）", player, text)
        return None

    log.info("【收到请求】玩家=%s 内容=%s", player, text)
    if full_message:
        log.debug("【聊天原文】%s", full_message)

    # ---- AI 之前的前置硬拦截：黑名单词根本不会送到模型那里 ----
    blocked = find_blocked_keyword(text)
    if blocked:
        log.warning("【前置拦截】玩家=%s 的请求命中绝对禁止关键字「%s」", player, blocked)
        say(player, f"这个请求包含禁用操作「{blocked}」，我不会做。", "red")
        return None

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(player, cfg)},
        {"role": "user", "content": f"玩家 {player} 对你说：{text}"},
    ]

    try:
        client = _get_client(cfg)
    except RuntimeError as exc:
        log.error("AI 客户端初始化失败：%s", exc)
        say(player, "我的大脑还没接上线，请管理员检查配置。", "red")
        return None

    max_rounds = int(runtime["max_tool_rounds"])
    answer: Optional[str] = None
    tool_called = False

    for round_index in range(1, max_rounds + 1):
        try:
            response = client.chat.completions.create(
                model=_model_name(cfg),
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=float(runtime["temperature"]),
                max_tokens=int(runtime["max_tokens"]),
                stream=False,
            )
        except Exception as exc:            # 网络超时、额度不足、密钥错误……全兜住
            log.error("调用大模型失败（第 %s 轮）：%s: %s", round_index, type(exc).__name__, exc)
            say(player, "我有点头晕（AI 服务异常），稍后再问我吧。", "red")
            return None

        # ---- 取出模型这一轮的回复 ----
        try:
            choice = response.choices[0]
            reply_message = choice.message
        except (AttributeError, IndexError) as exc:
            log.error("模型返回结构异常：%s（原始：%r）", exc, response)
            say(player, "AI 回了一句我听不懂的话，稍后再试。", "red")
            return None

        tool_calls = list(getattr(reply_message, "tool_calls", None) or [])

        # 没有工具调用 = 模型直接给出了最终答复
        if not tool_calls:
            answer = str(getattr(reply_message, "content", "") or "").strip()
            break

        tool_called = True
        log.info("模型请求调用 %s 个工具（第 %s 轮）", len(tool_calls), round_index)

        # 必须把 assistant 的 tool_calls 消息回填进历史，否则下一次请求会报错
        messages.append(
            {
                # content 原样回填（模型调工具时通常是 null，不要硬塞空串）
                "role": "assistant",
                "content": getattr(reply_message, "content", None),
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments or "{}",
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
        )

        # 逐个执行工具，并把结果作为 tool 消息回灌
        for tool_call in tool_calls:
            try:
                name = tool_call.function.name
                raw_args = tool_call.function.arguments
            except AttributeError:
                log.error("tool_call 结构异常：%r", tool_call)
                continue
            result = dispatch_tool(name, raw_args, player)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(tool_call, "id", "") or name,
                    "content": result,
                }
            )
    else:
        # for 循环正常跑完（没 break）= 一直在调工具，强制收尾
        log.warning("工具调用轮次达到上限 %s，强制结束本轮对话", max_rounds)

    # ---- 收尾：决定这句回复要不要发到游戏里 ----
    if answer:
        log.info("【管家回复】%s", answer)
        if not tool_called:
            # 没有调用工具 = 纯聊天/解释/拒绝，这句话直接进游戏聊天栏
            say(player, answer, "white")
        else:
            # 已经调用过工具 = 工具内部已经给玩家发过反馈，避免刷屏只留在控制台
            log.debug("本轮已通过工具向玩家反馈，最终文本仅记录不重复发送")
    else:
        log.warning("模型没有给出任何回复文本（玩家=%s）", player)

    return answer


# ================================================================ 六、命令行自测
if __name__ == "__main__":
    # 用法：python agent_core.py <玩家名> "<请求内容>"
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 3:
        print("用法：python agent_core.py <玩家名> <请求内容>")
        print('示例：python agent_core.py Steve "现在服务器有几个人"')
        sys.exit(1)

    argv_player = sys.argv[1]
    argv_message = " ".join(sys.argv[2:])
    process_player_message(argv_player, argv_message)
