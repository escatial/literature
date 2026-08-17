"""学术数据源模块:统一的 AcademicSource 协议 + 各数据源实现。"""
from retrieval.sources.base import AcademicSource, SourcePage
from retrieval.sources.openalex import OpenAlexSource
from retrieval.sources.pubmed import PubMedSource
from retrieval.sources.cnki import CNKISource

__all__ = [
    "AcademicSource", "SourcePage",
    "OpenAlexSource", "PubMedSource", "CNKISource",
]
