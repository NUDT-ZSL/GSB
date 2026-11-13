"""临时核验脚本：直接查看修复版响应体中的转义回显。"""

import html
import urllib.parse
import urllib.request

MARKER = 'z1qf03eb163x<svc>"1'
url = "http://127.0.0.1:8034/reflect?q=" + urllib.parse.quote(MARKER, safe="")
body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
print("修复版 /reflect 原始未转义标记出现:", MARKER in body)
print("转义形式出现:", html.escape(MARKER, quote=True) in body)
idx = body.find("你输入的是")
print("回显片段:", body[idx:idx + 100])
