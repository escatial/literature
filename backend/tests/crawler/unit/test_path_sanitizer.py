# -*- coding: utf-8 -*-
"""写路径安全(sanitize_filename / secure_write_path / user_output_path)专项。

覆盖 Mimosa 门禁"路径穿越"高危项的回归保护:
- `../../etc`、`..\\..\\`、盘符注入被拦(不可借穿越逃出原目录树)
- 合法中文题名不受影响
- save_results / save_failed 端到端仍按既有语义落盘
"""
import json

import pytest

from automation.cnki import crawler


class TestSanitizeFilename:
    """文件名净化:目录成分/盘符/非法字符剥离,中文保留。"""

    def test_strips_parent_segments(self):
        # ../ 与 ..\ 的目录成分整体剥离,只留安全末段
        assert crawler.sanitize_filename("../../etc/passwd") == "passwd"
        assert crawler.sanitize_filename("..\\..\\evil.json") == "evil.json"

    def test_strips_drive_letter(self):
        assert crawler.sanitize_filename("C:evil.json") == "evil.json"
        assert crawler.sanitize_filename("C:\\evil\\x.json") == "x.json"

    def test_empty_falls_back_to_default(self):
        assert crawler.sanitize_filename("..") == "cnki_file"
        assert crawler.sanitize_filename("...") == "cnki_file"
        assert crawler.sanitize_filename("") == "cnki_file"

    def test_chinese_title_untouched(self):
        title = "面向文献综述的检索增强生成研究"
        assert crawler.sanitize_filename(f"{title}.json") == f"{title}.json"

    def test_removes_windows_reserved_chars(self):
        assert crawler.sanitize_filename('a<b>c|d?e*f"g.json') == "abcdefg.json"


class TestSecureWritePath:
    """写路径加固:穿越段丢弃,受控目录意图保留。"""

    def test_traversal_converges_inside_tree(self, tmp_path):
        out = crawler.secure_write_path(str(tmp_path / "a" / ".." / ".." / "escape.json"))
        assert ".." not in out.parts
        assert out.name == "escape.json"

    def test_traversal_relative(self):
        out = crawler.secure_write_path("../../etc/passwd")
        assert ".." not in out.parts
        assert out.name == "passwd"

    def test_windows_traversal_relative(self):
        out = crawler.secure_write_path("..\\..\\outside.json")
        assert ".." not in out.parts
        assert out.name == "outside.json"

    def test_base_dir_forces_containment(self, tmp_path):
        out = crawler.secure_write_path("whatever.json", tmp_path)
        assert out.parent == tmp_path.resolve()

    def test_base_dir_rejects_empty_name(self, tmp_path):
        with pytest.raises(ValueError):
            crawler.secure_write_path("..", tmp_path)

    def test_isolated_absolute_dir_preserved(self, tmp_path):
        # 测试隔离目录(crawler_env 把运行时文件指到 tmp_path)语义保持
        target = tmp_path / "cookies.json"
        assert crawler.secure_write_path(str(target)) == target


class TestUserOutputPath:
    """用户输出路径:目录意图保留,空名自动命名。"""

    def test_auto_name_for_empty_name(self):
        out = crawler.user_output_path("..", default_prefix="result")
        assert out.name.startswith("result_")
        assert out.suffix == ".json"

    def test_chinese_and_subdir_preserved(self, tmp_path):
        out = crawler.user_output_path(str(tmp_path / "结果" / "综述.json"))
        assert out == tmp_path / "结果" / "综述.json"

    def test_traversal_segments_dropped(self):
        out = crawler.user_output_path("a/../../b.json")
        assert ".." not in out.parts
        assert out.name == "b.json"


class TestSaveEndpoints:
    """save_results / save_failed 端到端:穿越被拦 + 既有往返语义保持。"""

    def test_save_results_traversal_cannot_escape(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        results = [{"title": "t", "url": "u"}]
        crawler.save_results(results, "../../outside.json")
        # 落点收敛到 CWD 内,绝不写出 CWD 之外
        assert (tmp_path / "outside.json").exists()

    def test_save_results_roundtrip_with_chinese_name(self, tmp_path):
        results = [{"title": "深度学习", "authors": ["张三"], "url": "http://x"}]
        out = tmp_path / "深度综述.json"
        crawler.save_results(results, str(out))
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert loaded[0]["title"] == "深度学习"

    def test_save_failed_writes_after_sanitizing(self, tmp_path):
        p = tmp_path / "failed.json"
        crawler.save_failed([{"url": "http://x", "error": "e"}], str(p))
        assert p.exists()
        crawler.save_failed([], str(p))
        assert not p.exists()

    def test_save_cookies_converges_to_data_dir(self, tmp_path, monkeypatch):
        # cookie 是包内运行时文件:注入穿越段也强制收敛到 data 目录内
        monkeypatch.chdir(tmp_path)
        crawler.save_cookies({"k": "v"}, "..\\..\\cookies.json")
        target = crawler._DATA_DIR / "cookies.json"
        assert target.exists()
        assert not (tmp_path / "cookies.json").exists()
        target.unlink()  # 清理,不向包目录残留测试产物
