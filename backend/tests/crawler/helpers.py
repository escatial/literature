# -*- coding: utf-8 -*-
"""测试辅助:知网页面 HTML 生成器与通用断言工具。"""


def make_list_html(rows, total=None, page_extra=""):
    """生成知网列表页 HTML。

    rows: [(title, href, quote), ...];quote 为 None 时该行无 td.quote 单元。
    total: 检索结果总数(em.pagerTitleCell),默认 len(rows)。
    """
    trs = []
    for i, (title, href, quote) in enumerate(rows, start=1):
        quote_td = f'<td class="quote">{quote}</td>' if quote is not None else "<td></td>"
        trs.append(
            f'<tr><td class="name"><a class="fz14" target="_blank" href="{href}">{title}</a></td>'
            f"{quote_td}</tr>"
        )
    total = total if total is not None else len(rows)
    return (
        '<html><body><div id="countPageDiv" class="result-con-r">'
        f'<span class="pagerTitleCell"><span>共找到</span><em>{total}</em>'
        '<span>条结果</span></span></div>'
        f'<table id="gridTable">{"".join(trs)}</table>{page_extra}</body></html>'
    )


def make_list_rows(prefix, count, start_no=1, dbcode="CAPJ"):
    """批量生成 (title, href, quote) 行;quote 带 $[N] 序号(知网原始形态)。"""
    rows = []
    for i in range(start_no, start_no + count):
        title = f"{prefix}研究{i:04d}"
        href = f"/kcms2/article/abstract?v=abc&filename=P{i:04d}&dbcode={dbcode}"
        quote = f"$[{i}] 张三, 李四. {title}[J]. 测试学报, 2024, (1): {i}-{i + 1}."
        rows.append((title, href, quote))
    return rows


def make_detail_html(
    title="测试文献标题",
    authors=("张三", "李四"),
    orgs=("测试大学",),
    abstract="这是一段用于测试的摘要内容,足够长以通过完整性校验。",
    keywords=("个人信贷", "风险管控"),
    funds=("国家自然科学基金(12345678)",),
    source="测试学报",
    doi="10.12345/test.2024.001",
    with_gbt_hidden=True,
    export_url="/dm8/API/GetExport",
    export_id="P0001",
    abstract_text="",
):
    """生成 kcms2 风格详情页 HTML。abstract_text 传入时用 input#abstract_text 承载。"""
    author_links = "".join(f'<a href="javascript:;">{a}<sup>1</sup></a>' for a in authors)
    org_links = "".join(
        f'<a href="javascript:;">{i + 1}. {o}</a>' for i, o in enumerate(orgs)
    )
    kw_links = "".join(f'<a href="javascript:;">{k};</a>' for k in keywords)
    fund_spans = "".join(f"<span><a>{f}</a></span>" for f in funds)
    abs_block = (
        f'<input id="abstract_text" type="hidden" value="{abstract_text}"/>'
        if abstract_text
        else '<div id="ChDivSummary"><span class="abstract-text">'
        f"{abstract}</span></div>"
    )
    hidden = (
        f'<input id="export-url" type="hidden" value="{export_url}"/>'
        f'<input id="export-id" type="hidden" value="{export_id}"/>'
        if with_gbt_hidden
        else ""
    )
    return f"""<html><head><title>{title}</title>
    <meta name="description" content="{abstract}"/></head>
    <body>
    <div class="wx-tit"><h1>{title}</h1>
      <h3 class="author" id="authorpart">{author_links}</h3>
      <h3 class="author">{org_links}</h3>
    </div>
    {abs_block}
    <p class="keywords">{kw_links}</p>
    <p class="funds">{fund_spans}</p>
    <li><span class="rowtit">DOI</span><p>{doi}</p></li>
    <li><span class="rowtit">专辑</span><p>经济与管理</p></li>
    <li><span class="rowtit">专题</span><p>金融</p></li>
    <li><span class="rowtit">分类号</span><p>F830</p></li>
    <li><span class="rowtit">在线公开时间</span><p>2024-01-15</p></li>
    <a href="https://navi.cnki.net/knavi/journals/TEST/detail">测试学报</a>
    {hidden}
    </body></html>"""


def make_gbt_response(citations):
    """构造 GB/T 导出 API 响应体;citations 为 GB/T 7714-2025 条目列表。"""
    return {
        "code": 1,
        "data": [{"key": "GB/T 7714-2025", "value": list(citations)}],
    }


def script_details(responses):
    """把响应序列包装成 MockKNS.details 可调用:依次弹出,弹完 sticky 最后一个。

    元素可为 str(默认 200 text/html)或 (status, ctype, payload) 元组。
    """
    it = iter(responses)
    last = responses[-1]

    def _call():
        r = next(it, last)
        if isinstance(r, str):
            return (200, "text/html; charset=utf-8", r)
        return tuple(r)

    return _call


# ========================== 常用页面片段 ==========================
EMPTY_SHELL_HTML = (
    '<html><body><div id="gridTable" class="no-content">'
    '抱歉，暂无数据，请稍后重试。</div>'
    '<input type="hidden" name="grid" value=""/>'
    "</body></html>"
)

PARAM_INVALID_HTML = (
    '<html><body><input type="hidden" '
    'value="参数校验;字段【pageSize】校验失败"/>'
    "抱歉，暂无数据，请稍后重试。</body></html>"
)

CURSOR_OVERFLOW_HTML = (
    '<html><body><input type="hidden" '
    'value="start:21,size:20,total:1"/>起始游标越界'
    "抱歉，暂无数据，请稍后重试。</body></html>"
)

STRUCT_ERROR_HTML = (
    '<html><body><input type="hidden" '
    'value="查询对象结构错误！"/>'
    "抱歉，暂无数据，请稍后重试。</body></html>"
)

LOGIN_PAGE_HTML = (
    '<html><head><title>欢迎登录</title></head>'
    '<body><form action="/kns8s/login">用户登录</form></body></html>'
)

SECURITY_PAGE_HTML = (
    '<html><body>安全验证</body>'
    '<script src="/verify/home?captchaId=abc"></script></html>'
)

VERICODE_PAGE_HTML = (
    '<html><body><form id="vericodeForm">请输入验证码'
    '<img src="/verify/home?captchaId=xyz"/></form></body></html>'
)
