"""PrismClaw LLM 模型配置。基于 AgentScope OpenAIChatModel，接入自部署 dsv4。"""

import os
import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_model(cfg: dict, stream: bool = True):
    """构建 AgentScope 兼容的 LLM 模型实例。"""
    provider_name = cfg["llm"]["provider"]
    provider_cfg = cfg["providers"][provider_name]

    from agentscope.model import OpenAIChatModel

    generate_kwargs = {"temperature": cfg["llm"].get("temperature", 0.0)}

    # dsv4 thinking 控制（vLLM 用 chat_template_kwargs，字段名是 reasoning）
    enable_thinking = cfg["llm"].get("enable_thinking", False)
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
