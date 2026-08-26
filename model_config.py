"""PrismHarness LLM 模型配置。基于 AgentScope OpenAIChatModel，接入自部署 dsv4。"""

import os
import yaml


def _ensure_ssl_env():
    """修复 Windows/conda 下 SSL_CERT_FILE 指向不存在文件的崩溃。

    现象：conda 激活后常把 SSL_CERT_FILE 设成一个不存在的路径（例如
    `.../.conda/envs/<env>/ssl/cacert.pem`），导致 httpx 初始化时
    `ssl.create_default_context(cafile=...)` 直接 FileNotFoundError，
    进而 `OpenAIChatModel` 构造失败 → 所有对话/工具调用全部瘫痪。

    修复：若 SSL_CERT_FILE / SSL_CERT_DIR 指向不存在的路径，就把它们清掉，
    让 httpx 回落到自带的 certifi CA 包（始终有效）。路径存在则保留不动，
    不破坏用户的合法证书配置。
    """
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        val = os.environ.get(var, "")
        if val and not os.path.exists(val):
            os.environ.pop(var, None)
            print(f"[model_config] 清理无效的 {var}（指向不存在的文件）: {val}", flush=True)


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str = "config.yaml") -> dict:
    # 相对路径一律相对项目根（model_config.py 所在目录）解析，而不是 CWD。
    # 否则从别处启动 server 时，`load_config()` 会用 CWD 下的 config.yaml，
    # 和设置页写入的绝对 CONFIG_PATH 不是同一个文件 → 改模型配置毫无效果。
    if not os.path.isabs(path):
        path = os.path.join(_BASE_DIR, path)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_model(cfg: dict, stream: bool = True):
    """构建 AgentScope 兼容的 LLM 模型实例。"""
    _ensure_ssl_env()
    provider_name = cfg["llm"]["provider"]
    provider_cfg = cfg["providers"][provider_name]

    from agentscope.model import OpenAIChatModel

    generate_kwargs = {"temperature": cfg["llm"].get("temperature", 0.0)}

    # dsv4 thinking 控制（vLLM 用 chat_template_kwargs，字段名是 reasoning）
    # 优先取 provider 自己的 enable_thinking（设置页可按 provider 单独配），
    # 未配置时回落到顶层 llm.enable_thinking。
    enable_thinking = provider_cfg.get(
        "enable_thinking",
        cfg["llm"].get("enable_thinking", False),
    )
    generate_kwargs["extra_body"] = {
        "chat_template_kwargs": {"enable_thinking": enable_thinking}
    }

    return OpenAIChatModel(
        model_name=provider_cfg["model"],
        api_key=provider_cfg["api_key"],
        stream=stream,
        client_kwargs={"base_url": provider_cfg["api_base"]},
        generate_kwargs=generate_kwargs,
    )


def build_token_counter(cfg: dict):
    """构建官方 CompressionConfig 用的 token 计数器。

    用 OpenAI 兼容的 tiktoken 计数，与主模型 model_name 对齐。
    """
    from agentscope.token import OpenAITokenCounter

    provider_name = cfg["llm"]["provider"]
    provider_cfg = cfg["providers"][provider_name]
    return OpenAITokenCounter(model_name=provider_cfg["model"])
