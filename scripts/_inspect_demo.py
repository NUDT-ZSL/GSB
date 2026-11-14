"""模拟浏览器侧 decodeURIComponent + JSON.parse，核验预览内嵌数据。"""
from pathlib import Path
import json
import re
from urllib.parse import unquote

text = Path("out/demo/preview.html").read_text(encoding="utf-8")

for name in ("META", "SUMMARY", "BANDS"):
    m = re.search(r'const ' + name +
                  r' = JSON\.parse\(decodeURIComponent\("(.*?)"\)\);', text, re.S)
    data = json.loads(unquote(m.group(1)))
    print(name, "OK", type(data).__name__)

meta = json.loads(unquote(re.search(
    r'const META = JSON\.parse\(decodeURIComponent\("(.*?)"\)\);', text, re.S).group(1)))
print("META cells:", len(meta["cells"]), "top:", meta["cells"][0])
print("no raw double-quote inside embedded payloads:",
      all('"' not in m.group(1)
          for m in re.finditer(r'decodeURIComponent\("(.*?)"\)', text)))
