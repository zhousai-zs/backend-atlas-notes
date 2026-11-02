#!/usr/bin/env python3
import os, shutil, tarfile, html, io
import markdown
from pygments.formatters import HtmlFormatter
SRC = os.environ.get("ATLAS_SRC", "/workspace/atlas"); OUT = "docs"
rows = [l.rstrip("\n").split("\t") for l in open(f"{SRC}/order.txt", encoding="utf-8") if l.strip()]
L = [dict(n=int(r[0]), id=r[1], title=r[2], dom=r[3], stage=r[4]) for r in rows]
for x in L:
    p = f"{SRC}/lessons/{x['n']}-{x['id']}.md"; x["md"] = p if os.path.exists(p) else None
done = [x for x in L if x["md"]]
shutil.rmtree(OUT, ignore_errors=True); os.makedirs(f"{OUT}/lessons"); os.makedirs(f"{OUT}/files")
open(f"{OUT}/.nojekyll", "w").close()
def is_bin(p):
    if os.path.getsize(p) > 1 << 20: return True
    with open(p, "rb") as f: return b"\0" in f.read(8192)
def pack(n):
    for d in (f"{SRC}/practice{n}", f"{SRC}/lessons/practice{n}"):
        if os.path.isdir(d):
            dst = f"{OUT}/files/practice{n}.tar.gz"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                for root, ds, fs in os.walk(d):
                    ds[:] = sorted(x for x in ds if x not in (".git", "node_modules", "target", "__pycache__"))
                    for f in sorted(fs):
                        p = os.path.join(root, f)
                        if os.path.islink(p) or is_bin(p): continue
                        ti = t.gettarinfo(p, f"practice{n}/" + os.path.relpath(p, d)); ti.mtime = 0; ti.uid = ti.gid = 0; ti.uname = ti.gname = ""
                        with open(p, "rb") as fh: t.addfile(ti, fh)
            import gzip
            with open(dst, "wb") as o, gzip.GzipFile(fileobj=o, mode="wb", mtime=0, filename="") as g: g.write(buf.getvalue())
            return True
    tg = f"{SRC}/lessons/practice{n}.tar.gz"
    if os.path.exists(tg): shutil.copy(tg, f"{OUT}/files/practice{n}.tar.gz"); return True
    return False
light = HtmlFormatter(style="friendly").get_style_defs(".codehilite")
dark = HtmlFormatter(style="monokai").get_style_defs(".codehilite")
CSS = """:root{--bg:#fff;--fg:#222;--mut:#999;--side:#f6f7f9;--ln:#2563eb;--bd:#e5e7eb;--q:#f3f4f6}
@media(prefers-color-scheme:dark){:root{--bg:#16181d;--fg:#ddd;--mut:#666;--side:#1e2128;--ln:#7aa2ff;--bd:#333;--q:#22252c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.8 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
a{color:var(--ln);text-decoration:none}.wrap{display:flex}nav{width:300px;flex-shrink:0;height:100vh;overflow:auto;position:sticky;top:0;background:var(--side);padding:12px;font-size:14px;border-right:1px solid var(--bd)}
nav summary{cursor:pointer;font-weight:600}nav ul{list-style:none;padding-left:12px;margin:4px 0}nav li{line-height:1.6}.todo{color:var(--mut)}.todo em{font-size:12px}.cur{font-weight:700}
main{flex:1;min-width:0;max-width:880px;padding:24px 32px;margin:0 auto}pre{overflow:auto;padding:12px;border-radius:6px;font-size:14px;line-height:1.5}
.codehilite{border-radius:6px}code{font-family:"JetBrains Mono",Menlo,Consolas,monospace}table{border-collapse:collapse;display:block;overflow:auto}td,th{border:1px solid var(--bd);padding:6px 10px}
blockquote{margin:1em 0;padding:8px 16px;border-left:4px solid var(--ln);background:var(--q)}.pn{display:flex;justify-content:space-between;margin:32px 0;padding-top:16px;border-top:1px solid var(--bd)}
.dl a{display:inline-block;margin-right:12px;padding:4px 10px;border:1px solid var(--bd);border-radius:4px}.bar{height:10px;background:var(--bd);border-radius:5px}.bar div{height:100%;background:var(--ln);border-radius:5px}
@media(max-width:800px){.wrap{display:block}nav{width:auto;height:auto;position:static;max-height:none}nav>details:not([open]) ~ *{display:none}main{padding:16px}}
""" + light + "\n@media(prefers-color-scheme:dark){" + dark + "}"
open(f"{OUT}/style.css", "w").write(CSS)
def toc(cur, pre):
    o = ['<nav><details open id="toc"><summary>📚 Backend Atlas 目录（%d/%d）</summary><p><a href="%sindex.html">🏠 首页</a></p>' % (len(done), len(L), pre)]
    doms = []
    for x in L:
        if x["dom"] not in doms: doms.append(x["dom"])
    for d in doms:
        ls = [x for x in L if x["dom"] == d]
        op = " open" if any(x["n"] == cur for x in ls) or cur is None and any(x["md"] for x in ls) else ""
        o.append(f'<details{op}><summary>领域 {d}（{sum(1 for x in ls if x["md"])}/{len(ls)}）</summary>')
        for s in dict.fromkeys(x["stage"] for x in ls):
            o.append(f"<div>阶段 {s}</div><ul>")
            for x in ls:
                if x["stage"] != s: continue
                t = html.escape(f'{x["n"]}. {x["title"]}')
                if x["md"]: o.append(f'<li><a class="{"cur" if x["n"]==cur else ""}" href="{pre}lessons/{x["n"]}.html">{t}</a></li>')
                else: o.append(f'<li class="todo">{t} <em>待更新</em></li>')
            o.append("</ul>")
        o.append("</details>")
    o.append("</details></nav>"); return "".join(o)
def page(title, body, cur, pre):
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><link rel="stylesheet" href="{pre}style.css"><script>if(innerWidth<800)addEventListener("DOMContentLoaded",()=>document.getElementById("toc").open=false)</script></head><body><div class="wrap">{toc(cur,pre)}<main>{body}</main></div></body></html>'
for i, x in enumerate(done):
    src = open(x["md"], encoding="utf-8").read()
    body = markdown.markdown(src, extensions=["fenced_code", "tables", "codehilite", "toc", "sane_lists"], extension_configs={"codehilite": {"guess_lang": False}})
    shutil.copy(x["md"], f'{OUT}/files/{x["n"]}-{x["id"]}.md')
    dl = f'<a href="../files/{x["n"]}-{x["id"]}.md" download>⬇ 本课 Markdown</a>'
    if pack(x["n"]): dl += f'<a href="../files/practice{x["n"]}.tar.gz" download>⬇ 练习包 practice{x["n"]}.tar.gz</a>'
    pv = done[i-1] if i else None; nx = done[i+1] if i+1 < len(done) else None
    pn = '<div class="pn"><span>' + (f'<a href="{pv["n"]}.html">← {html.escape(pv["title"])}</a>' if pv else "") + "</span><span>" + (f'<a href="{nx["n"]}.html">{html.escape(nx["title"])} →</a>' if nx else "下一课 待更新") + "</span></div>"
    meta = f'<p style="color:var(--mut)">第 {x["n"]} 课 · 领域 {x["dom"]} · 阶段 {x["stage"]}</p><p class="dl">{dl}</p>'
    open(f'{OUT}/lessons/{x["n"]}.html', "w", encoding="utf-8").write(page(f'{x["n"]}. {x["title"]} - Backend Atlas', meta + body + pn, x["n"], "../"))
pct = len(done) * 100 / len(L)
rec = "".join(f'<li><a href="lessons/{x["n"]}.html">第 {x["n"]} 课 · {html.escape(x["title"])}</a></li>' for x in reversed(done[-10:]))
nxt = next((x for x in L if not x["md"]), None)
idx = f'<h1>Backend Atlas 后端学习笔记</h1><p>已完成 <b>{len(done)}/{len(L)}</b> 课（{pct:.1f}%）</p><div class="bar"><div style="width:{pct:.1f}%"></div></div>' + (f'<p>下一课：第 {nxt["n"]} 课 · {html.escape(nxt["title"])}（待更新）</p>' if nxt else "") + f'<h2>最近更新</h2><ul>{rec}</ul>'
open(f"{OUT}/index.html", "w", encoding="utf-8").write(page("Backend Atlas 后端学习笔记", idx, None, ""))
print(f"built {len(done)}/{len(L)} lessons")
