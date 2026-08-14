"""PrismHarness 功能开关。统一管理所有可选功能的启用/禁用。"""

FLAGS = {
    # 核心工具
    "enable_view_text_file": True,
    "enable_write_text_file": True,
    "enable_insert_text_file": True,
    "enable_execute_shell_command": True,
    "enable_download": True,

    # 高级功能
    "enable_websearch": True,
    "enable_subagent": False,
    "enable_cron": True,
    "enable_sandbox": False,

    # 技能系统
    "enable_skills": True,
    "enable_skill_auto_discover": True,
}

# 需要人工确认的工具 (PrismHarnessGuard 会拦截这些)
GUARD_TOOLS = [
    "write_text_file",
    "insert_text_file",
    "run_shell",
    "download_file",
]

# 高风险工具等待用户确认的超时时间(秒)，超时后自动拒绝，防止进程永久挂起
GUARD_TIMEOUT = 60
