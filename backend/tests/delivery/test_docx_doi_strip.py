# -*- coding: utf-8 -*-
"""D1 回归:review 导出 docx 的参考文献表不得出现 DOI/PubMed 链接。

用户硬性要求:生成的文档(正文/脚注/参考文献表)不允许出现 DOI 标识。
corpus 导出路径(api/papers.py::_strip_doi_marks)一直有剥;review 导出的
英文条目(citeproc/OpenAlex 渲染)此前直通泄漏 —— 第七轮导出产物
[25]-[48] 带 DOI:10.xxx 与 https://pubmed.ncbi.nlm.nih.gov/... 链接。

修复收口点 = delivery/docx_renderer.render_docx_with_footnotes 的 refs
拆行处:脚注定义与末尾参考文献列表段同源自 fn_items,单点覆盖两个出口
(corpus 条目已剥过,再过一遍幂等无害)。
"""
import zipfile

from delivery.docx_renderer import render_docx_with_footnotes, _strip_doi_marks


_REFS = "\n".join([
    "[1] Ho J. Sustainable shrimp protein supply chains. Aquaculture, 2022. "
    "DOI:10.1016/j.aquaculture.2022.738101.",
    "[2] Smith A. Traceability in seafood value chains. Food Control, 2023. "
    "https://pubmed.ncbi.nlm.nih.gov/36000000/",
    "[3] Lee K. Consumer preferences for traceable seafood. J Agric Econ, "
    "2021. https://doi.org/10.1111/1234.5678",
    "[4] 张三. 水产品品牌建设路径研究. 水产经济, 2022.",
])


def _docx_text(path) -> str:
    with zipfile.ZipFile(path) as z:
        return "".join(
            z.read(n).decode("utf-8")
            for n in z.namelist() if n.endswith(".xml")
        )


def test_strip_doi_marks_variants():
    """单元:DOI 前缀/doi.org/PubMed URL 三形态整段剥离,其余逐字保留。"""
    assert _strip_doi_marks(
        "Ho J. Aquaculture, 2022. DOI:10.1016/j.aqua.2022.738101."
    ) == "Ho J. Aquaculture, 2022."
    assert _strip_doi_marks(
        "Smith A. Food Control, 2023. https://pubmed.ncbi.nlm.nih.gov/36000000/"
    ) == "Smith A. Food Control, 2023."
    assert _strip_doi_marks(
        "Lee K. J Agric Econ, 2021. https://doi.org/10.1111/1234.5678"
    ) == "Lee K. J Agric Econ, 2021."
    assert _strip_doi_marks("张三. 水产品品牌建设路径研究. 水产经济, 2022.") == (
        "张三. 水产品品牌建设路径研究. 水产经济, 2022."
    )


def test_docx_export_refs_have_no_doi_or_links(tmp_path):
    """集成:含 DOI+PubMed 的英文条目渲染导出 → 解包 docx 全文(正文+
    脚注+参考文献表)无 DOI 标识与链接;正常条目原样保留。"""
    out = tmp_path / "review.docx"
    result = render_docx_with_footnotes(
        md_text="# 文献综述\n\n本章梳理水产品营销的国际研究进展。",
        out_path=str(out),
        papers=[],
        title="文献综述",
        refs_text=_REFS,
    )
    assert result.reference_count == 4
    blob = _docx_text(out)
    for banned in ("DOI", "doi", "doi.org", "pubmed", "10.1016", "10.1111", "36000000"):
        assert banned not in blob, f"导出产物泄漏 {banned}"
    assert "水产品品牌建设路径研究" in blob, "正常条目不得被误伤"
    assert "Aquaculture, 2022." in blob, "剥 DOI 后条目主体保留"
