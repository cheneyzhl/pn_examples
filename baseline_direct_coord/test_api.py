# -*- coding: utf-8 -*-
"""最简单的 API 连通性测试：向大模型发 hello，并打印回复。"""
import json
import urllib.request
import urllib.error

import config

def main():
    # 必须带 /v1/chat/completions 路径，否则会返回非 JSON（如 404 页面）
    url = config.BASE_URL.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": config.MODEL,
        "messages": [{"role": "user", "content": "hello"}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.API_KEY,
        },
        method="POST",
    )
    print("请求 URL:", url)
    print("模型:", config.MODEL)
    print("发送: hello")
    print("-" * 40)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", None)
            print("HTTP 状态:", status or "(urllib 未提供)")
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            print("错误: 响应不是合法 JSON —", e)
            print("原始响应（前 500 字符）:", raw[:500] if raw else "(空)")
            return
        content = (out.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print("回复:", content or "(空)")
        print("API 连通正常。")
    except urllib.error.HTTPError as e:
        print("HTTP 错误:", e.code, e.reason)
        print("响应体:", e.read().decode("utf-8", errors="replace")[:500])
    except urllib.error.URLError as e:
        print("连接失败:", e)
    except Exception as e:
        print("错误:", e)


if __name__ == "__main__":
    main()
