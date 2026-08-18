"""提示词模板管理模块。

支持两种组织形式:
1. 单文件:`<template_id>.md`,YAML 头(可选) + Markdown body
2. Suite:`<suite_id>.md` 一个文件管多个 template,使用 ` ```section <id>` 块定义 section
   (```_base` 段内含 `### {{base:<rule-id>}}` 块定义共享规则),通过
   `PromptTemplate.load("suite_id:section_id")` 加载。模板正文里用 `{{base:<rule-id>}}`
   引用共享规则,加载时自动展开。

约定: 所有模板文件存放在本目录下。
"""
from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent
_CACHE: dict[str, "PromptTemplate"] = {}


class PromptTemplate:
    """提示词模板。

    用法:
        # 单文件加载
        tpl = PromptTemplate.load("humanizer-zh")
        sys_prompt = tpl.render(text="...", score_mode=True)

        # Suite section 加载(命名空间语法)
        tpl = PromptTemplate.load("literature-review:section")
        sys_prompt = tpl.render(topic="...", section_title="...", ...)
    """

    def __init__(self, template_id: str, params: dict, body: str):
        self.id = template_id
        self.params = params
        self.body = body

    @classmethod
    def load(cls, template_id: str) -> "PromptTemplate":
        if template_id in _CACHE:
            return _CACHE[template_id]
        # Suite 语法: "suite_id:section_id"
        if ":" in template_id:
            suite_id, section_id = template_id.split(":", 1)
            return cls._load_suite_section(suite_id, section_id)
        # 单文件语法:文件名 = template_id
        path = _TEMPLATE_DIR / f"{template_id}.md"
        if not path.exists():
            raise FileNotFoundError(f"prompt template not found: {path}")
        text = path.read_text(encoding="utf-8")
        params, body = cls._parse_front_matter(text)
        tpl = cls(template_id, params, body)
        _CACHE[template_id] = tpl
        return tpl

    @classmethod
    def _load_suite_section(cls, suite_id: str, section_id: str) -> "PromptTemplate":
        cache_key = f"{suite_id}:{section_id}"
        if cache_key in _CACHE:
            return _CACHE[cache_key]
        path = _TEMPLATE_DIR / f"{suite_id}.md"
        if not path.exists():
            raise FileNotFoundError(f"suite not found: {path}")
        text = path.read_text(encoding="utf-8")
        parsed = cls._parse_suite(text)
        if section_id not in parsed["sections"]:
            available = ", ".join(parsed["sections"].keys())
            raise KeyError(
                f"section '{section_id}' not in suite '{suite_id}'. Available: {available}"
            )
        # 解析 {{base:<rule-id>}} 引用(嵌套解析最多 3 层)
        body = parsed["sections"][section_id]
        base_rules = parsed["base_rules"]
        for _ in range(3):
            resolved = re.sub(
                r"\{\{base:([\w-]+)\}\}",
                lambda m: base_rules.get(m.group(1), m.group(0)),
                body,
            )
            if resolved == body:
                break
            body = resolved
        tpl = cls(cache_key, parsed["meta"], body)
        _CACHE[cache_key] = tpl
        return tpl

    @staticmethod
    def _parse_front_matter(text: str) -> tuple[dict, str]:
        """解析文件头 YAML 风格 ```meta 块(suite 和单文件都走这里)。"""
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

    @staticmethod
    def _parse_suite(text: str) -> dict:
        """解析 suite 文件:meta 块 + 命名 sections + _base 段里的共享规则。

        Section 块用 ```` ```section <id>` ... ``` ` 标记,
        _base 段内的规则用 `### {{base:<rule-id>}}` 标记。

        返回: {
            "meta": {...},            # 来自 meta 块
            "base_rules": {...},      # 从 _base 段里提取
            "sections": {...},        # 所有 ```section <id>``` 块(含 _base)
        }
        """
        params, body = PromptTemplate._parse_front_matter(text)
        if "suite" not in params:
            raise ValueError("not a suite file (missing 'suite' in meta block)")

        # 提取所有 ```section <id> ... ``` 块
        sec_re = re.compile(
            r"^```section[ \t]+([\w-]+)[ \t]*\n(.*?)^```[ \t]*$",
            re.DOTALL | re.MULTILINE,
        )
        sections: dict[str, str] = {}
        for m in sec_re.finditer(body):
            sections[m.group(1)] = m.group(2).strip()

        # 从 _base 段提取 ```base <rule-id>``` 规则块
        base_rules: dict[str, str] = {}
        if "_base" in sections:
            rule_re = re.compile(
                r"^###\s+\{\{base:([\w-]+)\}\}\s*\n(.*?)(?=^###\s|\Z)",
                re.DOTALL | re.MULTILINE,
            )
            for m in rule_re.finditer(sections["_base"]):
                base_rules[m.group(1)] = m.group(2).strip()

        return {"meta": params, "base_rules": base_rules, "sections": sections}

    def render(self, **vars) -> str:
        """用 vars 替换 {{var}} 占位符。

        - 占位符列表从 body 静态扫描,不依赖 meta YAML
        - {{base:<rule-id>}} 已在 load() 阶段展开,这里不重复处理
        - 未提供且 body 里出现的占位符 → 抛错
        """
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
    """列出所有可用模板 id。

    - 单文件:`<id>`
    - Suite section:`<suite_id>:<section_id>`
    """
    out: list[str] = []
    for path in _TEMPLATE_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        params, _ = PromptTemplate._parse_front_matter(text)
        if "suite" in params:
            parsed = PromptTemplate._parse_suite(text)
            for section_id in parsed["sections"]:
                out.append(f"{params['suite']}:{section_id}")
        else:
            out.append(path.stem)
    return sorted(out)
