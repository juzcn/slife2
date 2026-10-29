# -*- coding: utf-8 -*-
"""Markdown (with \( \) math) -> printable HTML -> PDF via Headless Chrome + KaTeX."""
import os, re, sys, subprocess, urllib.request

MD = sys.argv[1] if len(sys.argv) > 1 else r"C:\OneDrive - cueb.edu.cn\Current\fintech\第九讲.md"
PDF = os.path.splitext(MD)[0] + ".pdf"
WORK = os.path.join(os.environ.get("TEMP", r"C:\Users\juz_c\AppData\Local\Temp"), "l9pdf")
KDIR = os.path.join(WORK, "katex")
FDIR = os.path.join(KDIR, "fonts")
os.makedirs(FDIR, exist_ok=True)

CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
TITLE = "第九讲 人工智能的起点与困局：智能是否只是规则加搜索"


def fetch(url, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return
    with urllib.request.urlopen(url, timeout=60) as r, open(dst, "wb") as f:
        f.write(r.read())


# 1) KaTeX assets (offline, so Chrome never waits on the network)
fetch(CDN + "/katex.min.css", os.path.join(KDIR, "katex.min.css"))
fetch(CDN + "/katex.min.js", os.path.join(KDIR, "katex.min.js"))
fetch(CDN + "/contrib/auto-render.min.js", os.path.join(KDIR, "auto-render.min.js"))
css = open(os.path.join(KDIR, "katex.min.css"), encoding="utf-8").read()
for name in sorted(set(re.findall(r"fonts/([A-Za-z0-9_\-]+\.(?:woff2|woff|ttf))", css))):
    try:
        fetch(CDN + "/fonts/" + name, os.path.join(FDIR, name))
    except Exception as e:
        print("font skip", name, e)

# 2) markdown -> HTML body (pandoc keeps \( \) and \[ \] for the math renderer)
body_html = os.path.join(WORK, "body.html")
subprocess.run(["pandoc", MD, "-f", "markdown+tex_math_single_backslash", "-t", "html5",
                "--wrap=none", "--no-highlight", "-o", body_html], check=True)
body = open(body_html, encoding="utf-8").read()

CSS = r"""
@page { size: A4; margin: 17mm 16mm 16mm 16mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Source Han Sans SC", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.72; color: #1b1b1b; margin: 0;
  text-align: justify; word-break: break-word;
}
h1, h2, h3, h4 { font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  color: #12395e; page-break-after: avoid; line-height: 1.35; }
h1 { font-size: 21pt; margin: 0 0 .5em; padding-bottom: .35em; border-bottom: 2.5px solid #12395e; }
h2 { font-size: 15pt; margin: 1.15em 0 .55em; padding-left: .5em; border-left: 5px solid #2f7fc1; }
h3 { font-size: 12.2pt; margin: 1em 0 .45em; color: #1d5c8f; }
h4 { font-size: 11pt; margin: .9em 0 .4em; }
p { margin: .55em 0; }
strong { color: #0d2f4d; }
a { color: #0b63c5; text-decoration: none; }
ul, ol { margin: .5em 0; padding-left: 1.9em; }
li { margin: .22em 0; }
blockquote {
  margin: .8em 0; padding: .55em .9em; background: #f3f7fb;
  border-left: 4px solid #7fb0dc; color: #24405a;
}
blockquote p { margin: .25em 0; }
hr { border: 0; border-top: 1px solid #d6dee7; margin: 1.1em 0; }
code {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: .9em; background: #f0f3f7; padding: .1em .3em; border-radius: 3px;
}
pre {
  background: #f6f8fa; border: 1px solid #e1e7ee; border-radius: 5px;
  padding: .7em .9em; overflow: visible; page-break-inside: avoid; margin: .7em 0;
}
pre code { background: none; padding: 0; font-size: 8.8pt; line-height: 1.5; white-space: pre-wrap; }
table {
  border-collapse: collapse; width: 100%; margin: .8em 0;
  font-size: 9.4pt; page-break-inside: avoid;
}
thead { display: table-header-group; }
th, td { border: 1px solid #cfd8e3; padding: .34em .5em; text-align: left; vertical-align: top; }
th { background: #eaf1f8; color: #12395e; font-weight: 600; }
tbody tr:nth-child(even) { background: #fafcfe; }
img { max-width: 100%; }
span.math.inline, .katex { font-size: 1.02em; }
.math.display, .katex-display { display: block; margin: .7em 0; text-align: center;
  page-break-inside: avoid; }
.katex-display > .katex { font-size: 1.12em; }
p, li, blockquote { orphans: 3; widows: 3; }
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="file:///{kdir}/katex.min.css">
<style>{css}</style>
</head><body>
{body}
<script src="file:///{kdir}/katex.min.js"></script>
<script src="file:///{kdir}/auto-render.min.js"></script>
<script>
window.addEventListener('DOMContentLoaded', function () {{
  renderMathInElement(document.body, {{
    delimiters: [
      {{left: '$$', right: '$$', display: true}},
      {{left: '\\\\[', right: '\\\\]', display: true}},
      {{left: '\\\\(', right: '\\\\)', display: false}}
    ],
    throwOnError: false,
    strict: false,
    ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }});
}});
</script>
</body></html>
""".format(title=TITLE, kdir=KDIR.replace("\\", "/"), css=CSS, body=body)

html_path = os.path.join(WORK, "lecture9.html")
open(html_path, "w", encoding="utf-8").write(HTML)
print("html:", html_path)

# 3) print to PDF with Headless Chrome
if os.path.exists(PDF):
    os.remove(PDF)
cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
       "--no-pdf-header-footer", "--virtual-time-budget=30000",
       "--user-data-dir=" + os.path.join(WORK, "chrome-profile"),
       "--print-to-pdf=" + PDF,
       "file:///" + html_path.replace("\\", "/")]
r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
print("chrome rc", r.returncode)
print((r.stderr or "")[-800:])
print("pdf exists:", os.path.exists(PDF), os.path.getsize(PDF) if os.path.exists(PDF) else 0)
