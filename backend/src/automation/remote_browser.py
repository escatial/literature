"""远程交互式浏览器会话服务(服务器部署架构)。

核心设计:
- 服务器端永远无头(headless),不弹任何本地窗口
- 页面截图通过 WebSocket 以 JPEG base64 实时推给前端
- 前端画布上的鼠标/键盘事件回传后端,在真实页面上执行
- 这样部署到 Linux 服务器时,用户通过网页即可完成知网/维普/万方的人机验证

协议(WebSocket /api/automation/ws/{session_id}):
  服务端 -> 客户端:
    {"type": "frame",   "data": "<jpeg base64>", "url": "...", "title": "..."}
    {"type": "status",  "data": {"verification": true, "vtype": "slider", ...}}
    {"type": "error",   "data": "..."}
  客户端 -> 服务端:
    {"action": "click",  "x": 120, "y": 340}            # 画布坐标(已按视口换算)
    {"action": "move",   "x": 120, "y": 340}
    {"action": "down",   "x": 120, "y": 340}
    {"action": "up",     "x": 120, "y": 340}
    {"action": "type",   "text": "hello"}
    {"action": "key",    "key": "Enter"}
    {"action": "scroll", "dx": 0, "dy": 300}
    {"action": "goto",   "url": "https://..."}
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from uuid import uuid4

from playwright.async_api import async_playwright, Page

from src.automation.browser_automation import ScholarBrowser, VerificationType

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 800}
FRAME_INTERVAL = 0.5      # 截图推流间隔(秒)
FRAME_QUALITY = 60        # JPEG 质量
SESSION_TTL = 600         # 会话空闲超时(秒)


@dataclass
class BrowserSession:
    """一个远程浏览器会话。

    支持多个目标 URL 的轮询:用户给定一组数据库入口URL,会话按 current_index
    指示当前激活哪个。前端通过 WS 推送的 frame 里同时携带当前激活库名。
    """
    session_id: str
    page: Page
    targets: list[str] = field(default_factory=list)   # 所有要轮询的 URL
    db_types: list[str] = field(default_factory=list)  # 与 targets 一一对应的库名
    current_index: int = 0                            # 当前激活的库在 targets 里的索引
    last_active: float = 0.0
    streaming: bool = False
    verification: bool = False
    vtype: str = "none"
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def current_db(self) -> str | None:
        if 0 <= self.current_index < len(self.db_types):
            return self.db_types[self.current_index]
        return None

    @property
    def current_url(self) -> str | None:
        if 0 <= self.current_index < len(self.targets):
            return self.targets[self.current_index]
        return None


class RemoteBrowserManager:
    """管理所有远程浏览器会话(单例)。"""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> None:
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

    async def create_session(
        self,
        targets: list[str] | str,
        db_types: list[str] | str = "cnki",
    ) -> BrowserSession:
        """创建会话:新开一个无头页面并导航到首个目标 URL。

        参数兼容两种签名:
            create_session(url, db_type)             # 单库
            create_session([url1,url2,url3], ["cnki","wanfang","cqvip"])  # 多库轮询
        """
        if isinstance(targets, str):
            targets = [targets]
        if isinstance(db_types, str):
            db_types = [db_types]
        if not targets:
            raise ValueError("targets 不能为空")
        if len(db_types) == 1 and len(targets) > 1:
            db_types = [db_types[0]] * len(targets)
        if len(db_types) != len(targets):
            raise ValueError("targets 与 db_types 长度不一致")

        await self._ensure_browser()
        context = await self._browser.new_context(
            viewport=VIEWPORT,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()
        try:
            # 使用 networkidle 保证真正渲染完成(避免知网那种"半截页"残留)
            await page.goto(targets[0], wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(1.0)
        except Exception as e:
            log.warning("初始导航失败(继续): %s", e)

        sid = uuid4().hex[:12]
        session = BrowserSession(
            session_id=sid, page=page, targets=list(targets), db_types=list(db_types), current_index=0,
        )
        session.last_active = asyncio.get_event_loop().time()
        async with self._lock:
            self._sessions[sid] = session

        # 检测验证
        await self._refresh_verification(session, db_types[0])
        return session

    async def switch_target(self, session_id: str, index: int) -> dict:
        """切换到指定的 URL 索引,导航浏览器过去。"""
        session = self.get(session_id)
        if not session:
            return {"ok": False, "reason": "session not found"}
        if not (0 <= index < len(session.targets)):
            return {"ok": False, "reason": f"index {index} out of range"}
        if index == session.current_index:
            return {"ok": True, "unchanged": True}
        session.current_index = index
        url = session.targets[index]
        try:
            await session.page.goto(url, wait_until="networkidle", timeout=45_000)
            await asyncio.sleep(1.0)
            await self._refresh_verification(session, session.db_types[index])
            return {"ok": True, "db": session.current_db, "url": url}
        except Exception as e:
            log.warning("切换目标失败: %s", e)
            return {"ok": False, "reason": str(e)}

    async def advance_to_next_db(self, session_id: str) -> dict:
        """前进到下一个库,若已是最后一个则标记完成。"""
        session = self.get(session_id)
        if not session:
            return {"ok": False, "reason": "session not found"}
        next_idx = session.current_index + 1
        if next_idx >= len(session.targets):
            return {"ok": False, "exhausted": True, "reason": "已是最后一个库"}
        r = await self.switch_target(session_id, next_idx)
        return {"ok": r.get("ok", False), "new_index": next_idx, "db": session.current_db}

    async def fill_query_into_search_box(
        self,
        session_id: str,
        query: str,
        submit: bool = True,
        restrict_to_journals: bool = True,
        use_advanced: bool = True,
    ) -> dict:
        """把检索式填入当前库的单输入框(简单可靠)。

        策略:
          1) 找页面上**第一个可见的文本输入框**(通用 selector)
          2) 点击 + 清空 + 填入 query
          3) 如果 submit=True → 按 Enter

        不再尝试自动进高级检索/勾期刊 - 那些都对 DOM 太脆,反而失败。
        期刊限定由"默认 URL"承担:会话创建时,如果用户想限定期刊,前端在 URL 上加参数。
        """
        import time
        t0 = time.time()
        session = self.get(session_id)
        if not session:
            log.warning("[fill_query] sid=%s not found", session_id)
            return {"ok": False, "reason": "session not found"}
        db = session.current_db or "cnki"
        log.info("[fill_query] start sid=%s db=%s query=%r", session_id, db, query[:80])
        page = session.page

        # 通用找框:页面上第一个可见的文本输入框
        box = None
        selectors = [
            "input[type='text']:visible",
            "input[type='search']:visible",
            "input:not([type]):visible",
            "textarea:visible",
            "#txt_SearchText",      # 知网旧版
            "input#txt_search",
            "input.ipt",
            "input.search-input",
            "input.search-text",
            "input#id_term",        # pubmed
            "input[name='q']",      # openalex / google
            "input[placeholder*='检索']",
            "input[placeholder*='Search']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    box = loc
                    log.info("[fill_query] 找到输入框: %s", sel)
                    break
            except Exception:
                continue

        if not box:
            return {"ok": False, "reason": f"未在{db}页面找到任何输入框"}

        try:
            await asyncio.wait_for(box.click(), timeout=3.0)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")

            # 知网高级检索是"多行 AND"模型,query="(A+B)*(C+D)*(E+F)" 表示 3 行 AND。
            # 拆分:每个 * 是一行,内容是去掉括号后用 + 串联的同义词。
            parts = []
            for p in query.split("*"):
                p = p.strip().strip("()").strip()
                if p:
                    parts.append(p)
            if not parts:
                parts = [query]

            if db == "cnki" and len(parts) > 1:
                # 知网高级检索:每行 = 一个"主题"输入框。
                # 策略:JS 一次性克隆、追加 N-1 个新行,直接给每个 input 赋值。
                try:
                    n_to_add = len(parts) - 1
                    # JS 一次完成:追加 N-1 行,并把所有"主题"输入框的 value 设为对应 part。
                    # 同时把 select 全部设为"主题"。
                    log.info("[fill_query] cnki 多行: parts=%d, n_to_add=%d", len(parts), n_to_add)
                    filled = await page.evaluate("""
                        (args) => {
                            const parts = args.parts;
                            // 1) 找"主题"行容器
                            const all = Array.from(document.querySelectorAll('input[type=text], input:not([type]), textarea'));
                            if (all.length === 0) return {ok: false, reason: 'no inputs'};
                            const first = all[0];
                            let row = first.closest('tr, li, .row, .item, .search-row, [class*=row]');
                            if (!row) row = first.parentElement;
                            if (!row || !row.parentElement) return {ok: false, reason: 'no row container'};
                            // **重要:先清空原"主题"行的所有 input 值**,避免之前残留
                            row.querySelectorAll('input, textarea').forEach(el => { el.value = ''; });
                            row.querySelectorAll('select').forEach(s => { s.selectedIndex = 0; });
                            // 同时清空其他"作者/文献来源"等行,避免被填到非主题框
                            const allRows = Array.from(document.querySelectorAll('tr, li, .row, .item, .search-row, [class*=row]'))
                                .filter(r => r.querySelector('input[type=text], input:not([type]), textarea'));
                            allRows.forEach(r => {
                                r.querySelectorAll('input, textarea').forEach(el => { el.value = ''; });
                            });
                            // 2) 追加 n_to_add 个新行
                            for (let i = 0; i < args.n_to_add; i++) {
                                const clone = row.cloneNode(true);
                                clone.querySelectorAll('input, textarea').forEach(el => { el.value = ''; });
                                clone.querySelectorAll('select').forEach(s => { s.selectedIndex = 0; });
                                // 删除非第一个 input 槽位(让"作者/文献来源"等不会出现在新行)
                                const ins = clone.querySelectorAll('input[type=text], input:not([type]), textarea');
                                for (let k = 1; k < ins.length; k++) {
                                    let n = ins[k];
                                    while (n && n.parentElement && n.parentElement !== clone) {
                                        n = n.parentElement;
                                    }
                                    if (n && n.parentElement) n.remove();
                                }
                                row.parentElement.appendChild(clone);
                            }
                            // 3) 现在按"主题"行的 input 顺序填值
                            //    找所有"主题"行(每个 select 是主题的行的第一个 input)
                            const rows = Array.from(document.querySelectorAll('tr, li, .row, .item, .search-row, [class*=row]'))
                                .filter(r => r.querySelector('input[type=text], input:not([type]), textarea'));
                            const results = [];
                            for (let i = 0; i < rows.length && i < parts.length; i++) {
                                const inputs = rows[i].querySelectorAll('input[type=text], input:not([type]), textarea');
                                if (inputs.length > 0) {
                                    inputs[0].value = parts[i];
                                    // 触发 input 事件,让知网感知到值变了
                                    inputs[0].dispatchEvent(new Event('input', {bubbles: true}));
                                    inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                                    results.push({row: i, value: parts[i].slice(0, 20), id: inputs[0].id || '(no id)'});
                                }
                            }
                            return {ok: true, rows: rows.length, filled: results};
                        }
                    """, {"parts": parts, "n_to_add": n_to_add})
                    log.info("[fill_query] cnki JS 填表结果: %s", filled)
                    if not filled.get("ok"):
                        raise RuntimeError(filled.get("reason", "unknown"))
                except Exception as e:
                    log.warning("[fill_query] cnki 多行失败, 退化单行: %s", e)
                    await asyncio.wait_for(box.fill(query), timeout=5.0)
            else:
                # 普通库/单行:直接填整段
                await asyncio.wait_for(box.fill(query), timeout=5.0)

            if submit:
                # 优先找"检索"按钮;否则 Enter
                try:
                    btn = page.locator("button:has-text('检索'), .btn:has-text('检索')").first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                    else:
                        await page.keyboard.press("Enter")
                except Exception:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(2.0)
            elapsed = time.time() - t0
            log.info("[fill_query] done in %.2fs -> ok, rows=%d", elapsed, len(parts))
            return {"ok": True, "db": db, "query": query, "rows": len(parts), "submitted": submit, "elapsed": round(elapsed, 2)}
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "填入超时"}
        except Exception as e:
            log.warning("填检索式失败: %s", e)
            return {"ok": False, "reason": str(e)}

    async def _fill_cnki_advanced_rows(
        self,
        page,
        query: str,
        submit: bool = True,
        restrict_to_journals: bool = True,
    ) -> dict:
        """知网高级检索:按 '*' 拆多行,逐行填"主题"字段。

        步骤(已重构,逐行 evaluate):
          1) 把 query 拆 parts
          2) 第一次 evaluate:拿初始 inputs / selects / "+" 按钮 / 检索按钮
          3) 如果行数 < parts 数,优先点页面原生"+"按钮加足行(每次点完重新 evaluate)
          4) 逐行 evaluate 获取该行真实 input id,然后 fill
          5) 点"检索"
        """
        try:
            # 1) 拆检索式
            parts = []
            for p in query.split("*"):
                p = p.strip().strip("()").strip()
                if p:
                    parts.append(p)
            if not parts:
                return {"ok": False, "reason": "检索式为空"}
            log.info("CNKI 拆 %d 行:%s", len(parts), parts)
            need = len(parts)

            # 2) 第一次 evaluate
            info = await self._eval_cnki_rows(page)
            log.info("CNKI 探查 #1:%s", info)
            if not info.get("inputs"):
                return await self._fill_single_box_fallback(page, query, submit)

            # 3) 加足行(优先用页面"+"按钮;每次点完重新 evaluate)
            current_inputs = info["inputs"]
            add_btn = info.get("add_buttons") or []
            for _ in range(need - len(current_inputs)):
                clicked = False
                for btn in add_btn:
                    try:
                        if btn.get("id"):
                            await page.click(f"#{btn['id']}")
                        else:
                            # 文本"+" / "添加" / "增加"
                            await page.locator(f"{btn['tag']}:has-text('+')").first.click()
                        await asyncio.sleep(0.4)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    # 兜底:JS 克隆第一行
                    await page.evaluate("""
                        () => {
                            const candidates = document.querySelectorAll('input[type=text], textarea');
                            if (candidates.length === 0) return false;
                            const first = candidates[0].closest('div, tr, li, section, article') || candidates[0].parentElement;
                            if (!first || !first.parentElement) return false;
                            const clone = first.cloneNode(true);
                            // 清空 input value
                            clone.querySelectorAll('input, textarea').forEach(i => i.value = '');
                            clone.querySelectorAll('select').forEach(s => s.selectedIndex = 0);
                            first.parentElement.appendChild(clone);
                            return true;
                        }
                    """)
                    await asyncio.sleep(0.4)
                # 重新探查
                info = await self._eval_cnki_rows(page)
                current_inputs = info["inputs"]
                if not add_btn:
                    add_btn = info.get("add_buttons") or []

            log.info("CNKI 已有 %d 行,需要 %d 行", len(current_inputs), need)

            # 4) 逐行填入
            for i, part in enumerate(parts):
                # 重新 evaluate 得到该 i 行的 id
                info = await self._eval_cnki_rows(page)
                inputs = info["inputs"]
                selects = info["selects"]
                if i >= len(inputs):
                    log.warning("第 %d 行无 input 可填", i)
                    continue
                ipt = inputs[i]
                sel = selects[i] if i < len(selects) else None
                # 选 SU 字段
                if sel and sel.get("id"):
                    try:
                        await page.select_option(f"#{sel['id']}", "SU")
                    except Exception:
                        pass
                # 填文本
                if ipt.get("id"):
                    try:
                        await page.fill(f"#{ipt['id']}", part)
                    except Exception as e:
                        log.warning("fill #%s 失败: %s", ipt["id"], e)
                else:
                    # 通过 nth 兜底
                    try:
                        await page.locator("input[type=text]").nth(i).fill(part)
                    except Exception as e:
                        log.warning("nth fill %d 失败: %s", i, e)
                log.info("CNKI 填 row %d: %s", i, part)

            # 5) 点"检索"
            if submit:
                info = await self._eval_cnki_rows(page)
                clicked = False
                for btn in info.get("search_buttons") or []:
                    try:
                        if btn.get("id"):
                            await page.click(f"#{btn['id']}")
                        else:
                            await page.locator(f"{btn['tag']}:has-text('{btn.get('text','')}')").first.click()
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    # 兜底按 Enter
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
                await asyncio.sleep(2.5)

            return {"ok": True, "db": "cnki", "rows": need, "query": query, "submitted": submit}
        except Exception as e:
            log.warning("CNKI 多行填表失败: %s", e)
            return {"ok": False, "reason": str(e)}

    async def _eval_cnki_rows(self, page) -> dict:
        """知网高级检索页 DOM 探查:inputs / selects / 按钮。

        返回 {"inputs": [{id, cls}, ...], "selects": [...], "add_buttons": [...], "search_buttons": [...]}。
        """
        return await page.evaluate(
            """
            () => {
                const allInputs = Array.from(document.querySelectorAll('input[type=text], input:not([type]), textarea'));
                const inputs = allInputs
                    .filter(i => i.offsetParent !== null)   // 可见
                    .map(i => ({id: i.id, cls: (i.className||'').slice(0,40), name: i.name||''}));
                const allSelects = Array.from(document.querySelectorAll('select'));
                const selects = allSelects
                    .filter(s => s.offsetParent !== null)
                    .map(s => ({id: s.id, cls: (s.className||'').slice(0,40), name: s.name||''}));
                // 找"+"按钮(可见)
                const addBtns = Array.from(document.querySelectorAll('a, button, span, i, [class*=add], [class*=plus]'))
                    .filter(b => {
                        if (b.offsetParent === null) return false;
                        const t = (b.innerText || b.value || '').trim();
                        return t === '+' || t === '＋' || /\\+/.test(t) || /增加|添加|plus|addrow/i.test((b.className||'') + ' ' + (b.id||''));
                    })
                    .map(b => ({tag: b.tagName, text: (b.innerText||b.value||'').slice(0,10), id: b.id||'', cls: (b.className||'').slice(0,40)}));
                // 找"检索"按钮
                const searchBtns = Array.from(document.querySelectorAll('button, input[type=button], input[type=submit], a.btn, .btn'))
                    .filter(b => {
                        if (b.offsetParent === null) return false;
                        const t = (b.innerText || b.value || '').trim();
                        return /检索|搜索|search|submit/i.test(t) || /search|submit/i.test((b.className||'') + ' ' + (b.id||''));
                    })
                    .map(b => ({tag: b.tagName, text: (b.innerText||b.value||'').slice(0,10), id: b.id||'', cls: (b.className||'').slice(0,40)}));
                return {inputs, selects, add_buttons: addBtns, search_buttons: searchBtns};
            }
            """
        )

    async def _fill_single_box_fallback(self, page, query: str, submit: bool) -> dict:
        """高级检索页结构没找到时,退回单输入框。"""
        try:
            box = page.locator("input[type='text'], input[type='search']").first
            if await box.count() == 0:
                return {"ok": False, "reason": "找不到任何输入框"}
            await box.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await box.fill(query)
            if submit:
                await page.keyboard.press("Enter")
                await asyncio.sleep(2.0)
            return {"ok": True, "db": "cnki", "fallback": True, "query": query, "submitted": submit}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    async def _ensure_cnki_advanced(self, page) -> bool:
        """确保知网在"高级检索"页面。"""
        try:
            url = page.url
            if "AdvSearch" in url or "adv" in url.lower():
                return True
            # 找页面上的"高级检索"链接
            link = page.locator("a:has-text('高级检索'), a:has-text('高级搜索')").first
            if await link.count() > 0 and await link.is_visible():
                await link.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                return True
            # 否则直接 goto 高级检索
            await page.goto("https://kns.cnki.net/kns8s/AdvSearch",
                            wait_until="networkidle", timeout=30_000)
            return True
        except Exception as e:
            log.warning("跳高级检索失败: %s", e)
            return False

    async def _restrict_to_journals(self, page, db: str) -> bool:
        """中文库限定为期刊文献。

        - cnki: 默认只勾"期刊"(去除其他复选框)
        - cqvip: 在"学科/期刊/学位"等过滤中保留期刊
        - wanfang: 在"期刊/学位/会议"等过滤中保留期刊
        """
        try:
            if db == "cnki":
                return await self._cnki_journal_only(page)
            elif db == "cqvip":
                return await self._cqvip_journal_only(page)
            elif db == "wanfang":
                return await self._wanfang_journal_only(page)
        except Exception as e:
            log.warning("勾选期刊失败(%s): %s", db, e)
        return False

    async def _cnki_journal_only(self, page) -> bool:
        """知网:把"来源类型"区域里只勾"期刊"。

        关键:知网高级检索页的"来源类型"复选框文本可能是:
          - "期刊" / "期刊论文" / "中英文文献"(多个) 等
        我们对所有 label/checkbox 文本进行分类:
          - 是"期刊"相关 → 勾上
          - 不是"期刊"相关(学位论文/会议/报纸/图书/成果/年鉴/标准/专利) → 取消
        """
        keep = {"期刊", "期刊论文", "中英文文献"}
        drop = {"学位论文", "AI赋能学位论文", "会议", "报纸", "整报出版", "图书", "图书全文", "成果", "年鉴", "标准", "专利", "同文识学", "词典"}
        ok = False
        try:
            # 策略 1:遍历所有 label 找关联 input
            labels = page.locator("label")
            count = await labels.count()
            for i in range(count):
                lab = labels.nth(i)
                text = (await lab.inner_text()).strip()
                if not text:
                    continue
                if not any(k in text for k in (list(keep) + list(drop))):
                    continue
                for_attr = await lab.get_attribute("for")
                target = None
                if for_attr:
                    try:
                        target = page.locator(f"#{for_attr}").first
                    except Exception:
                        target = None
                if target is None or await target.count() == 0:
                    target = lab.locator("input[type='checkbox'], input[type='radio']").first
                if target and await target.count() > 0:
                    checked = await target.is_checked()
                    is_keep = any(k == text or k in text for k in keep)
                    if is_keep:
                        if not checked:
                            await target.check()
                        ok = True
                    else:
                        if checked:
                            await target.uncheck()

            # 策略 2:兜底 — 直接按 input 邻近文本处理
            if not ok:
                cbs = page.locator("input[type='checkbox']")
                cb_count = await cbs.count()
                for j in range(cb_count):
                    cb = cbs.nth(j)
                    # 找 label 或父元素文本
                    try:
                        cb_id = await cb.get_attribute("id")
                        if cb_id:
                            text = await page.locator(f"label[for='{cb_id}']").first.inner_text()
                        else:
                            text = await cb.evaluate("el => el.parentElement ? el.parentElement.innerText : ''")
                    except Exception:
                        continue
                    text = (text or "").strip()
                    if not text:
                        continue
                    is_keep = any(k in text for k in keep)
                    is_drop = any(k in text for k in drop)
                    if not (is_keep or is_drop):
                        continue
                    checked = await cb.is_checked()
                    if is_keep and not checked:
                        await cb.check()
                    if is_drop and checked:
                        await cb.uncheck()
                    ok = True
        except Exception as e:
            log.warning("知网期刊过滤失败: %s", e)
        return ok

    async def _cqvip_journal_only(self, page) -> bool:
        """维普:在范围筛选里保留"期刊"分类。"""
        try:
            # 维普高级检索有"仅期刊"复选框
            for sel in [
                "input[type='checkbox']:near(:text('期刊'))",
                "label:has-text('期刊') input[type='checkbox']",
            ]:
                els = page.locator(sel)
                if await els.count() > 0:
                    if not await els.first.is_checked():
                        await els.first.check()
                    return True
            # 维普简易方案:在 URL 上加  &from=  类限定
            return False
        except Exception:
            return False

    async def _wanfang_journal_only(self, page) -> bool:
        """万方:在文献类型里仅勾"期刊"。"""
        try:
            labels = page.locator("label")
            count = await labels.count()
            for i in range(count):
                lab = labels.nth(i)
                text = (await lab.inner_text()).strip()
                if text in {"期刊", "期刊论文", "学位论文", "会议论文", "科技报告", "标准", "专利"}:
                    target = lab.locator("input").first
                    if target and await target.count() > 0:
                        checked = await target.is_checked()
                        if text in {"期刊", "期刊论文"}:
                            if not checked:
                                await target.check()
                        else:
                            if checked:
                                await target.uncheck()
            return True
        except Exception:
            return False

    async def _refresh_verification(self, session: BrowserSession, db_type: str = "cnki") -> None:
        """用既有的验证识别逻辑判断当前页是否需要人工验证。"""
        probe = ScholarBrowser(headless=True)
        probe._page = session.page
        try:
            result = await probe.detect_verification(db_type)
            session.verification = result.detected
            session.vtype = result.type.value if isinstance(result.type, VerificationType) else "none"
        except Exception as e:
            log.warning("验证检测失败: %s", e)
            session.verification = False
            session.vtype = "none"

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if not session:
            return
        try:
            await session.page.context.close()
        except Exception as e:
            log.warning("关闭会话失败: %s", e)

    async def shutdown(self) -> None:
        async with self._lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            await self.close_session(sid)
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # ─── 帧捕获与操作执行 ─────────────────────────────

    async def capture_frame(self, session: BrowserSession) -> dict:
        """截取当前页一帧,返回可 JSON 序列化的帧数据。"""
        buf = await session.page.screenshot(type="jpeg", quality=FRAME_QUALITY)
        return {
            "type": "frame",
            "data": base64.b64encode(buf).decode(),
            "url": session.page.url,
            "title": await session.page.title(),
            "current_db": session.current_db,
            "current_index": session.current_index,
            "targets_count": len(session.targets),
        }

    async def execute(self, session: BrowserSession, action: dict) -> None:
        """在页面上执行一个用户操作。"""
        page = session.page
        act = action.get("action")
        x, y = action.get("x", 0), action.get("y", 0)
        session.last_active = asyncio.get_event_loop().time()

        if act == "click":
            await page.mouse.click(x, y)
        elif act == "move":
            await page.mouse.move(x, y)
        elif act == "down":
            await page.mouse.move(x, y)
            await page.mouse.down()
        elif act == "up":
            await page.mouse.up()
        elif act == "type":
            await page.keyboard.type(str(action.get("text", "")), delay=30)
        elif act == "key":
            await page.keyboard.press(str(action.get("key", "")))
        elif act == "scroll":
            await page.mouse.wheel(action.get("dx", 0), action.get("dy", 0))
        elif act == "goto":
            await page.goto(str(action.get("url")), wait_until="domcontentloaded", timeout=30_000)
        else:
            log.warning("未知操作: %s", act)

    async def fetch_page_html(self, session: BrowserSession) -> str:
        """获取当前页 HTML 全文,供前端的提取流程使用。"""
        return await session.page.content()

    async def fetch_candidates(self, session: BrowserSession, db_type: str | None = None) -> list[dict]:
        """从当前页面抽取候选文献条目。

        db_type 默认使用会话当前的库(由 current_index 指向)。
        """
        from .html_extractor import extract_papers_from_html, make_lit_id

        target_db = db_type or session.current_db or "cnki"
        html = await self.fetch_page_html(session)
        items = extract_papers_from_html(html, base_url=session.page.url, db_type=target_db)
        prefix = target_db
        for it in items:
            it["lit_id"] = make_lit_id(prefix, it["title"], it.get("authors") or [], it.get("year"))
            it["source"] = "user_imported"  # 浏览器检索到的中文/英文文献都入中文库
            it["selected"] = True
        return items

    async def auto_extract(
        self,
        session: BrowserSession,
        target: int = 30,
        max_pages: int = 10,
        db_type: str | None = None,
        on_progress=None,
    ) -> dict:
        """自动循环翻页抽取,直到条数 >= target 或翻页达 max_pages。

        返回:
        {
          "items": [所有条目],
          "pages": 已抽的页数,
          "stopped_reason": "reached_target" | "max_pages" | "no_next" | "verification"
        }
        on_progress: async 回调,签名 async def(pages, count, url)
        """
        target_db = db_type or session.current_db or "cnki"
        collected: list[dict] = []
        seen_titles: set[str] = set()
        pages_done = 0
        stopped_reason = "max_pages"

        # 加载首屏
        try:
            await session.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        for page_no in range(1, max_pages + 1):
            pages_done = page_no
            await asyncio.sleep(0.6)  # 让前端的 frame 流跟上

            # 检测是否到了验证页
            await self._refresh_verification(session, target_db)
            if session.verification:
                stopped_reason = "verification"
                break

            html = await self.fetch_page_html(session)
            page_items = await asyncio.to_thread(self._extract_sync, html, session.page.url, target_db)

            new_added = 0
            for it in page_items:
                title = it.get("title", "")
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    collected.append(it)
                    new_added += 1
            if on_progress:
                try:
                    await on_progress(pages_done, len(collected), session.page.url)
                except Exception:
                    pass

            if len(collected) >= target:
                stopped_reason = "reached_target"
                break

            # 尝试点"下一页"
            clicked = await self._click_next_page(session, target_db)
            if not clicked:
                stopped_reason = "no_next"
                break

            # 等新页面
            try:
                await session.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

        return {
            "items": collected,
            "pages": pages_done,
            "count": len(collected),
            "stopped_reason": stopped_reason,
            "current_db": target_db,
        }

    @staticmethod
    def _extract_sync(html: str, base_url: str, db_type: str) -> list[dict]:
        from .html_extractor import extract_papers_from_html, make_lit_id
        raw = extract_papers_from_html(html, base_url=base_url, db_type=db_type)
        for it in raw:
            it["lit_id"] = make_lit_id(db_type, it["title"], it.get("authors") or [], it.get("year"))
            it["source"] = "user_imported"
            it["selected"] = True
        return raw

    async def _click_next_page(self, session: BrowserSession, db_type: str) -> bool:
        """尝试点击当前库的「下一页」按钮。返回是否成功。"""
        selectors = {
            "cnki": [
                ".pageBar .page-next",
                "a.next",
                "a:has-text('下一页')",
                "a:has-text('下页')",
                ".pages a[title='下一页']",
            ],
            "wanfang": [
                ".pages .next",
                "a.next",
                "a:has-text('下一页')",
            ],
            "cqvip": [
                "a.next",
                ".pagebar .next",
                "a:has-text('下一页')",
            ],
            "openalex": [
                "a[rel='next']",
                "a:has-text('Next')",
                "nav.pagination a:has-text('›')",
            ],
            "pubmed": [
                ".page-navigator .next",
                "a.next",
                "a:has-text('Next')",
            ],
        }.get(db_type, ["a:has-text('下一页')", "a:has-text('Next')", "a.next"])

        for sel in selectors:
            try:
                loc = session.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=3_000)
                    return True
            except Exception:
                continue
        return False


# 全局单例
manager = RemoteBrowserManager()
