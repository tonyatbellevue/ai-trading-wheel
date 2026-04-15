"""Claude AI 分析模块 — 对 Wheel 摘要做 AI 点评

用法：
    from claude_analyst import analyze
    extra_md = analyze(summary_markdown)

ANTHROPIC_API_KEY 未设置时静默返回空字符串，不影响主流程。
"""
from __future__ import annotations

from config import settings
from loguru import logger


def analyze(summary_md: str) -> str:
    """调用 Claude API 对 Wheel 摘要做 AI 分析，返回追加的 markdown 段落。"""
    if not settings.ANTHROPIC_API_KEY:
        return ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

        prompt = f"""你是一位专业的期权 Wheel 策略分析师。
以下是今天的 Wheel 策略运行报告（Markdown 格式）：

{summary_md}

请用中文写一段简洁的 AI 分析（200字以内），包括：
1. 当前持仓/阶段的风险点和机会
2. 近期新闻对当前标的的潜在影响（如有相关新闻）
3. 下一轮操作的具体建议（卖 Put 还是等待，选哪个候选）

输出格式：直接输出 markdown，以 `## AI 分析` 作为标题开头。不要解释你在做什么。"""

        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis = message.content[0].text.strip()
        logger.info("Claude AI 分析完成")
        return "\n\n" + analysis + "\n"

    except Exception as e:
        logger.warning(f"Claude 分析失败（跳过）: {e}")
        return ""
