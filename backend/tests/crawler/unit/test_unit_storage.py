# -*- coding: utf-8 -*-
"""单元测试:存储层(save_results JSON/CSV 往返 / save_failed / 断点续读取)。"""
import csv
import json

import pytest

from automation.cnki import crawler


SAMPLE = [{
    "title": "样本文献",
    "authors": ["张三", "李四"],
    "orgs": ["测试大学"],
    "source": "测试学报",
    "abstract": "摘要内容。",
    "keywords": ["信贷", "风控"],
    "funds": ["国家自科(123)"],
    "doi": "10.1234/s.1",
    "album": "经济",
    "topic": "金融",
    "clc_code": "F830",
    "publish_time": "2024-01-01",
    "url": "http://d/1",
}]


def test_save_results_json_roundtrip(crawler_env, tmp_path):
    out = tmp_path / "r.json"
    crawler.save_results(SAMPLE, str(out))
    data = json.loads(out.read_text("utf-8"))
    assert data == SAMPLE  # JSON 往返无损(列表字段保持 list)


def test_save_results_csv_roundtrip_with_normalize(crawler_env, tmp_path):
    out = tmp_path / "r.csv"
    crawler.save_results(SAMPLE, str(out))
    # CSV 文件列序 = CSV_FIELDS,utf-8-sig 头
    with open(out, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == crawler.CSV_FIELDS
        rows = list(reader)
    assert rows[0]["authors"] == "张三 | 李四"

    # 断点续读取 + 归一化:列表字段还原回 list,再保存等价
    loaded, urls = crawler.load_existing_results(str(out))
    assert urls == {"http://d/1"}
    norm = crawler._normalize_loaded_rows(loaded)
    assert norm[0]["authors"] == ["张三", "李四"]
    assert norm[0]["keywords"] == ["信贷", "风控"]
    out2 = tmp_path / "r2.csv"
    crawler.save_results(norm, str(out2))
    assert out.read_bytes() == out2.read_bytes()  # 归一化往返字节级等价


def test_save_results_empty_output_auto_name(crawler_env, tmp_path, monkeypatch):
    monkeypatch.setitem(crawler.CONFIG["paths"], "default_output_prefix", str(tmp_path / "auto"))
    monkeypatch.setattr(crawler.time, "time", lambda: 1700000000)
    crawler.save_results([], "")
    p = tmp_path / "auto_1700000000.json"
    assert p.exists()
    assert json.loads(p.read_text("utf-8")) == []


def test_save_failed_writes_and_clears(crawler_env, tmp_path):
    p = tmp_path / "failed.json"
    failed = [{"url": "http://d/1", "error": "超时"}]
    crawler.save_failed(failed, str(p))
    assert json.loads(p.read_text("utf-8")) == failed

    # 空清单 → 删除旧文件(防陈旧清单误导补抓)
    crawler.save_failed([], str(p))
    assert not p.exists()
    crawler.save_failed([], str(p))  # 再删一次不报错


def test_load_existing_results_missing_and_broken(crawler_env, tmp_path):
    assert crawler.load_existing_results("") == ([], set())
    assert crawler.load_existing_results(str(tmp_path / "no.json")) == ([], set())
    bad = tmp_path / "bad.json"
    bad.write_text("{不是JSON", "utf-8")
    assert crawler.load_existing_results(str(bad)) == ([], set())
