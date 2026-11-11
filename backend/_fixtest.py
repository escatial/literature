# -*- coding: utf-8 -*-
"""修正版:patch messages_create(无反斜杠锚点)。跑完即删。"""
p = 'tests/writing/test_custom_theme_axes.py'
lines = open(p, encoding='utf-8').read().splitlines(keepends=True)

out = []
skip_until_yield_done = False
for i, line in enumerate(lines):
    if 'def fake_stream(system, user, **kwargs):' in line:
        # 替换整个 fake_stream 定义 + monkeypatch 行 + groups 调用块,
        # 直到 classify_with_custom_axes 调用结束
        out.append('    def fake_create(system, user, **kwargs):\n')
        out.append('        lines = [\n')
        out.append('            f"{i}. " + ("品牌建设" if i <= 20 else "数字渠道")\n')
        out.append('            for i in range(1, 31)\n')
        out.append('        ]\n')
        out.append('        return chr(10).join(lines)\n\n')
        out.append('    monkeypatch.setattr(clf, "messages_create", fake_create)\n\n')
        out.append('    groups = clf.classify_with_custom_axes(\n')
        out.append('        papers, "水产品营销策略", ["品牌建设", "数字渠道"],\n')
        out.append('    )\n')
        # 跳过原块:fake_stream def → yield 行 → monkeypatch(llm) 行 → groups 调用(
        skip = True
        depth_done = False
        continue
    out.append(line)

open(p, 'w', encoding='utf-8').write(''.join(out))
print('done (manual review needed)')
