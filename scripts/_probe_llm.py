import os, sys, traceback
from dotenv import load_dotenv

BASE = r"d:\code\个人开发项目\202608\文献综述agent\backend"
sys.path.insert(0, os.path.join(BASE, "src"))
load_dotenv(os.path.join(BASE, ".env"))

print("BASE_URL=", os.environ.get("MINIMAX_BASE_URL"))
print("MODEL   =", os.environ.get("MINIMAX_MODEL"))
print("KEY head=", (os.environ.get("MINIMAX_API_KEY") or "")[:10], "len=", len(os.environ.get("MINIMAX_API_KEY") or ""))

from llm.client import messages_create, get_client

# 1) 普通对话
try:
    out = messages_create("You are a helpful assistant.", "用一句话介绍无人机协同配送。", max_tokens=200, temperature=0.3)
    print("PLAIN:", repr(out[:200]))
except Exception as e:
    print("PLAIN ERR:", repr(e))
    traceback.print_exc()

# 2) JSON schema response_format
try:
    import json
    from retrieval.intent import SearchIntent
    schema = SearchIntent.model_json_schema()
    rf = {"type": "json_schema", "json_schema": {"name": "search_intent", "schema": schema, "strict": True}}
    out = messages_create("output JSON", "topic: 无人机协同配送应急物资; year: 2026", max_tokens=2000, temperature=0.3, response_format=rf)
    print("JSON_LEN:", len(out or ""), "head:", repr((out or "")[:120]))
except Exception as e:
    print("JSON ERR:", repr(e))
    traceback.print_exc()