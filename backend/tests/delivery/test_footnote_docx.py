# -*- coding: utf-8 -*-
"""Word 导出渲染器回归:真实脚注 + 学位论文排版(宋体小四/Times/1.5 倍行距)。"""
import re
import zipfile

from delivery.docx_renderer import render_docx_with_footnotes

REFS = "\n".join(
    f"[{i}] 张三. 水产品营销研究{i}[J]. 测试学报, 2024({i % 12 + 1}): 10-20."
    for i in range(1, 9)
)
MD = (
    "## 一、主题A\n\n"
    "张三（2024）指出渠道变革影响终端效率[1]，李四（2024）比较了电商模式差异[2]。\n\n"
    "## 二、主题B\n\n"
    "王五（2023）补充了社群营销的边界条件[3]。"
)


def _build(tmp_path):
    out = tmp_path / "review.docx"
    return render_docx_with_footnotes(
        md_text=MD, out_path=str(out), papers=[], title="测试综述",
        refs_text=REFS,
    ), out


def test_footnotes_real_and_aligned(tmp_path):
    """每个 [N] 都替换为原生脚注引用;脚注定义与参考文献条目对齐。"""
    result, out = _build(tmp_path)
    z = zipfile.ZipFile(out)
    assert "word/footnotes.xml" in z.namelist()
    doc = z.read("word/document.xml").decode("utf-8")
    fn = z.read("word/footnotes.xml").decode("utf-8")
    refs = len(re.findall(r"<w:footnoteReference", doc))
    defs = len(re.findall(r'<w:footnote w:id="\d+">', fn))
    assert refs == 3
    assert defs >= 3
    # 正文中不再残留裸 [N](参考文献表的 [N] 是合法文本,不含脚注引用)
    body_texts = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", doc))
    assert "[1]" not in body_texts.split("参考文献")[0]


def test_thesis_typography(tmp_path):
    """宋体/_times new roman_ 小四 + 1.5 倍行距落在段落与 run 上。"""
    _, out = _build(tmp_path)
    z = zipfile.ZipFile(out)
    doc = z.read("word/document.xml").decode("utf-8")
    assert 'w:line="360"' in doc                      # 1.5 倍行距
    assert 'w:eastAsia="宋体"' in doc                  # 中文宋体
    assert "Times New Roman" in doc                   # 西文
    assert 'w:val="24"' in doc                        # 小四 12pt = sz 24
    fn = z.read("word/footnotes.xml").decode("utf-8")
    assert 'w:eastAsia="宋体"' in fn                  # 脚注同样中文宋体
    assert "Times New Roman" in fn


def test_refs_text_bypasses_citeproc(tmp_path):
    """导出场景:直接使用存储的编号清单,papers 为空也能渲染。"""
    result, out = _build(tmp_path)
    assert result.footnote_count == 8
    assert result.reference_count == 8
