"""提示词模板管理模块。

将外部 Claude Skills(humanizer-zh / literature-review)拆解、抽象为本项目可调用的
标准化提示词模板。每个模板:
- 用 YAML 前言定义参数和输入输出契约
- 用 Markdown body 描述行为准则和示例
- 通过 render(**vars) 注入实际参数,返回最终 system prompt

约定: 模板文件存放在此目录下,文件名 = template_id,
支持以纯文本 Markdown 或 YAML+Markdown 编写。
"""
from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent
_CACHE: dict[str, "PromptTemplate"] = {}


class PromptTemplate:
    """提示词模板。

    用法:
        tpl = PromptTemplate.load("humanizer-zh")
        sys_prompt = tpl.render(text="...", score=True)
    """

    def __init__(self, template_id: str, params: dict, body: str):
        self.id = template_id
        self.params = params
        self.body = body

    @classmethod
    def load(cls, template_id: str) -> "PromptTemplate":
        if template_id in _CACHE:
            return _CACHE[template_id]
        path = _TEMPLATE_DIR / f"{template_id}.md"
        if not path.exists():
            raise FileNotFoundError(f"prompt template not found: {path}")
        text = path.read_text(encoding="utf-8")
        params, body = cls._parse_front_matter(text)
        tpl = cls(template_id, params, body)
        _CACHE[template_id] = tpl
        return tpl

    @staticmethod
    def _parse_front_matter(text: str) -> tuple[dict, str]:
        """解析文件头 YAML 风格 ```meta 块。"""
        if text.startswith("```meta"):
            end = text.find("```\n", len("```meta"))
            if end < 0:
                return {}, text
            yaml_text = text[len("```meta"):end].strip()
            body = text[end + 4:]
            params: dict = {}
            for line in yaml_text.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                k, _, v = line.partition(":")
                params[k.strip()] = v.strip()
            return params, body
        return {}, text

    def render(self, **vars) -> str:
        """用 vars 替换 {{var}} 占位符。

        - 未在 vars 中提供且声明为 required 的占位符 → 抛错
        - 提供 None 的 required 参数 → 抛错
        - 占位符列表从 body 里的 {{xxx}} 静态扫描,不依赖 params YAML
        """
        # 静态扫描 body 找出所有 {{xxx}} 占位符
        required_placeholders = set(re.findall(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}", self.body))
        missing = [n for n in required_placeholders if vars.get(n) in (None, "")]
        if missing:
            raise ValueError(
                f"template '{self.id}' missing required params: {missing}"
            )

        def repl(m: re.Match) -> str:
            name = m.group(1).strip()
            if name not in vars:
                return m.group(0)
            return str(vars[name])

        return re.sub(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}", repl, self.body)


def list_templates() -> list[str]:
    """列出所有可用模板 id。"""
    return sorted(p.stem for p in _TEMPLATE_DIR.glob("*.md"))