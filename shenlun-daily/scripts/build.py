#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
申论日报 · 一键排版
=====================================================
一份 content.json 进，一份 PDF + 一张长图出。

用法
    python build.py content.json [-o 输出目录] [--ratio 2:3|a4|3:4]
                                 [--px 1080] [--no-pdf] [--no-png] [--keep-html]

环境
    python3（需 playwright + pymupdf + pillow，Chromium 已随 playwright 安装）
    依赖装在哪个解释器，就用那个解释器跑本脚本

设计要点
    1. 全篇只用 mm，字号一律由 --fs 推导 → 改一个值，整页等比缩放
    2. 二分调 --fs，让两栏内容刚好填满可用高度，绝不溢出、绝不留大片空白
    3. 正文支持轻标记：**加粗**  ==红下划线==  ^^淡红底^^  ___填空线___
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")

# 页面尺寸（宽固定 210mm，高按比例）
RATIOS = {
    "2:3": (210.0, 315.0),   # 小红书长图 / 原海报比例
    "a4":  (210.0, 297.0),
    "3:4": (210.0, 280.0),
}
PX_PER_MM = 96.0 / 25.4
FS_MIN, FS_MAX = 2.00, 4.80          # 正文基准字号搜索区间（mm）


# --------------------------------------------------------------- 文本处理
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def rich(s):
    """轻标记 → HTML。顺序：先转义，再替换标记。"""
    s = esc(s)
    # 填空线：2 个以上下划线，宽度随下划线数量变化
    s = re.sub(r"_{2,}", lambda m: '<span class="bk" style="width:%.2fem"></span>'
               % (len(m.group(0)) * 0.46), s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"==(.+?)==", r"<u>\1</u>", s)
    s = re.sub(r"\^\^(.+?)\^\^", r'<span class="term">\1</span>', s)
    s = re.sub(r"\[\[(.+?)\]\]", r"<em>\1</em>", s)
    return s


def plain(s):
    """去掉轻标记，用于字数统计与一致性校验。"""
    return re.sub(r"(\*\*|==|\^\^)", "", str(s))


def sec_head(tag, title="", note=""):
    out = ['<div class="sec">']
    if tag:
        out.append(f'<span class="tag">{esc(tag)}</span>')
    if title:
        out.append(f'<span class="t">{esc(title)}</span>')
    if note:
        out.append(f'<span class="n">{esc(note)}</span>')
    out.append("</div>")
    return "".join(out)


# --------------------------------------------------------------- 各区块
def render_header(m):
    meta = [esc(m.get("date", "")), esc(m.get("weekday", ""))]
    if m.get("issue"):
        meta.append(esc(m["issue"]))
    line = '<span class="sp">·</span>'.join(x for x in meta if x)
    tail = [x for x in [m.get("edition"), m.get("source")] if x]
    tail_html = ('<span class="sp">·</span>'
                 + '<span class="sp">·</span>'.join(esc(x) for x in tail)) if tail else ""
    logo = ""
    if m.get("logo", "offcn"):
        p = os.path.join(ASSETS, m.get("logo", "offcn") + ".png")
        if os.path.exists(p):
            logo = f'<img src="file://{p}" alt="">'
    brand = "".join(f"<b>{esc(x)}</b>" for x in m.get("brand", []))
    return (f'<header id="hd"><div class="brand">{brand}</div>'
            f'<div class="meta">{line}{tail_html}{logo}</div></header>')


def render_head(h):
    kw = ""
    if h.get("keywords"):
        kw = ('<div class="kw"><i>关键词</i>'
              + "".join(f"<span>{esc(k)}</span>" for k in h["keywords"]) + "</div>")
    cd = ""
    if h.get("countdown"):
        c = h["countdown"]
        cd = (f'<span class="cd">{esc(c.get("label", ""))}'
              f'<em>{esc(c.get("days", ""))}</em></span>')
    credit = f'<div class="credit"><span>{rich(h.get("credit", ""))}</span>{cd}</div>' if (h.get("credit") or cd) else ""
    fit = ""
    if h.get("fit"):
        rows = "".join(f'<div class="frow"><div class="k">{esc(r["k"])}</div>'
                       f'<div class="v">{rich(r["v"])}</div></div>' for r in h["fit"])
        fit = f'<aside class="fit">{rows}</aside>'
    return (f'<section id="head"><div class="lead">'
            f'<div class="kicker">{esc(h.get("kicker", ""))}</div>'
            f'<h1>{esc(h["title"])}</h1>'
            f'<div class="sub">{esc(h.get("subtitle", ""))}</div>'
            f'{kw}{credit}</div>{fit}</section>')


def render_left(L):
    note = L.get("note") or ""
    if not note:                      # 右上角自动补「全文 N 段 · 约 M 字」，header 不空着
        n = len(L["paras"])
        chars = sum(len(plain(p["text"]).replace(" ", "").replace("\n", "")) for p in L["paras"])
        note = f"全文 {n} 段 · 约 {chars} 字"
    out = [sec_head(L.get("tag", "原文"), L.get("title", ""), note)]
    for p in L["paras"]:
        out.append('<div class="p">')
        out.append('<div class="p-h">'
                   f'<span class="no">{esc(p["no"])}</span>'
                   f'<span class="tg">{esc(p["tag"])}</span>'
                   + (f'<span class="nt">〔{esc(p["note"])}〕</span>' if p.get("note") else "")
                   + '</div>')
        out.append('<p>' + "</p><p>".join(rich(x) for x in str(p["text"]).split("\n")) + '</p></div>')
    return "".join(out)


def render_right(R):
    out = [sec_head(R.get("tag", "拆解速览"), R.get("title", ""), R.get("note", ""))]
    if R.get("thesis"):
        out.append(f'<div class="thesis"><span class="lab">总论点</span>'
                   f'<p>{rich(R["thesis"])}</p></div>')
    for it in R["items"]:
        out.append('<div class="it"><div class="it-h">'
                   f'<span class="no">{esc(it["no"])}</span>'
                   f'<span class="tg">{esc(it["tag"])}</span></div>'
                   f'<div class="it-a">{rich(it["analysis"])}</div>')
        if it.get("quote"):
            fn = f'<span class="qf">{esc(it["func"])}</span>' if it.get("func") else ""
            out.append(f'<div class="q"><span class="qt">{rich(it["quote"])}</span>{fn}</div>')
        out.append("</div>")
    if R.get("summary"):
        out.append('<div class="sum"><span class="lab">总结</span>'
                   f'<p>{rich(R["summary"])}</p></div>')
    return "".join(out)


def render_bottom(B):
    out = ['<section id="bottom"><div class="tpl">']

    # 左：句式
    P = B.get("patterns") or {}
    if P:
        out.append('<div>' + sec_head(P.get("tag", "句式"), P.get("title", ""), P.get("note", "")))
        out.append('<div class="rows">')
        for r in P["rows"]:
            out.append(f'<div class="row"><span class="k">{esc(r["k"])}</span>'
                       f'<span class="v">{rich(r["v"])}</span></div>')
        out.append("</div>")
        # 图例贴在本栏底部：既补齐高度差，也紧挨它要解释的标记
        L = B.get("legend")
        if L:
            items = "".join(f"<span>{rich(x)}</span>" for x in L)
            out.append(f'<div class="legend"><span class="lb">'
                       f'{esc(B.get("legend_title", "笔记符号"))}</span>{items}</div>')
        out.append("</div>")

    # 右：换题
    R = B.get("remap") or {}
    if R:
        out.append('<div>' + sec_head(R.get("tag", "换题"), R.get("title", ""), R.get("note", "")))
        if R.get("topics"):
            out.append('<div class="topics">'
                       + '<i>/</i>'.join(esc(t) for t in R["topics"]) + '</div>')
        out.append('<div class="rows">')
        for r in R["rows"]:
            out.append(f'<div class="row"><span class="k">{esc(r["k"])}</span>'
                       f'<span class="v">{rich(r["v"])}</span></div>')
        out.append("</div></div>")

    out.append("</div>")  # .tpl

    # 金句带（全宽）
    G = B.get("gold") or {}
    if G:
        out.append('<div id="gold">' + sec_head(G.get("tag", "金句"), G.get("title", ""), G.get("note", "")))
        out.append('<div class="list">')
        for r in G["rows"]:
            out.append(f'<div class="g"><span class="no">{esc(r["no"])}</span>'
                       f'<span class="tx">{rich(r["text"])}</span>'
                       + (f'<span class="fn">{esc(r["func"])}</span>' if r.get("func") else "")
                       + (f'<span class="us">{esc(r["use"])}</span>' if r.get("use") else "")
                       + '</div>')
        out.append("</div></div>")

    # 笔记符号图例（句式栏缺失时的兜底位置）
    L = B.get("legend")
    if L and not B.get("patterns"):
        items = "".join(f"<span>{rich(x)}</span>" for x in L)
        out.append(f'<div class="legend"><span class="lb">'
                   f'{esc(B.get("legend_title", "笔记符号"))}</span>{items}</div>')

    out.append("</section>")
    return "".join(out)


def render_footer(f):
    logo = ""
    p = os.path.join(ASSETS, "offcn.png")
    if os.path.exists(p):
        logo = f'<img src="file://{p}" alt="">'
    city = f'<span>{esc(f["city"])}</span>' if f.get("city") else ""
    return (f'<footer id="ft"><span>{esc(f.get("left", ""))}</span>'
            f'<span class="r">{city}{logo}</span></footer>')



# --------------------------------------------------------------- 早报模板
def render_news(N):
    """左栏：今日时政，一条 = 编号 + 领域 + 标题 + 摘要 + 考点。"""
    out = [sec_head(N.get("tag", "今日时政"), N.get("title", ""), N.get("note", ""))]
    for it in N["items"]:
        out.append('<div class="ni"><div class="ni-h">'
                   f'<span class="no">{it["no"]}</span>'
                   f'<span class="ni-dom">{esc(it["dom"])}</span>'
                   f'<span class="ni-t">{rich(it["title"])}</span></div>'
                   f'<div class="ni-x">{rich(it["text"])}</div>'
                   f'<div class="ni-p"><b>考点</b><span>{rich(it["point"])}</span></div></div>')
    return "".join(out)


def render_side(cfg):
    """右栏：会议·文件要点 + 广东/大湾区素材。"""
    out = []
    M = cfg.get("meetings")
    if M:
        out.append(sec_head(M.get("tag", "会议·文件"), M.get("title", ""), M.get("note", "")))
        for m in M["items"]:
            out.append('<div class="mt"><div class="mt-n">'
                       f'<span class="no">{m["no"]}</span><b>{esc(m["name"])}</b>'
                       + (f'<i>{esc(m["when"])}</i>' if m.get("when") else "")
                       + '</div><ul>'
                       + "".join(f'<li>{rich(x)}</li>' for x in m["points"])
                       + '</ul></div>')
    R = cfg.get("region")
    if R:
        out.append(sec_head(R.get("tag", "广东 · 大湾区"), R.get("title", ""), R.get("note", "")))
        for r in R["items"]:
            out.append(f'<div class="ri"><span class="no">{r["no"]}</span>'
                       f'<span class="rt">{esc(r["tag"])}</span>'
                       f'<span class="rx">{rich(r["text"])}</span></div>')
    return "".join(out)


def render_brief_bottom(B):
    out = ['<section id="bottom">']
    G = B.get("gold")
    if G:
        out.append('<div class="bgold" style="margin-top:0;padding-top:0;border-top:none">'
                   + sec_head(G.get("tag", "今日金句"), G.get("title", ""), G.get("note", ""))
                   + '<div class="list">')
        for r in G["rows"]:
            out.append(f'<div class="g"><span class="no">{esc(r["no"])}</span>'
                       f'<span class="tx">{rich(r["text"])}</span>'
                       + (f'<span class="us">{esc(r["use"])}</span>' if r.get("use") else "")
                       + '</div>')
        out.append("</div></div>")
    F = B.get("figures")
    if F:
        out.append('<div class="bfig">'
                   + sec_head(F.get("tag", "数字速记"), F.get("title", ""), F.get("note", ""))
                   + '<div class="grid">')
        for f in F["items"]:
            out.append(f'<div class="f"><b>{esc(f["v"])}</b><span>{esc(f["k"])}</span></div>')
        out.append("</div></div>")
    out.append("</section>")
    return "".join(out)


def build_html(cfg, pw, ph):
    with open(os.path.join(HERE, "style.css"), encoding="utf-8") as fh:
        css = fh.read()
    head_font = cfg.get("font_head")
    if head_font:
        css = css.replace("'庞门正道标题体'", f"'{head_font}'")
    body_font = cfg.get("font_body")
    if body_font:
        css = css.replace("'PingFang SC'", f"'{body_font}'")

    brief = cfg.get("template") == "brief"
    if brief:                                  # 早报：时政条 + 会议 + 地方素材
        left_html, right_html = render_news(cfg["news"]), render_side(cfg)
        bottom_html, cls = render_brief_bottom(cfg.get("bottom", {})), "brief"
    else:                                      # 精读：一篇评论的九段拆解
        left_html, right_html = render_left(cfg["left"]), render_right(cfg["right"])
        bottom_html, cls = render_bottom(cfg.get("bottom", {})), ""

    body = (render_header(cfg.get("meta", {})) + render_head(cfg.get("head", {}))
            + '<section id="body"><div id="cols">'
            + f'<div id="colL"><div class="inner">{left_html}</div></div>'
            + f'<div id="colR"><div class="inner">{right_html}</div></div>'
            + '</div></section>'
            + bottom_html
            + render_footer(cfg.get("footer", {})))
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{esc(cfg["head"]["title"])}</title>
<style>
@page{{size:{pw}mm {ph}mm;margin:0;}}
html,body{{width:{pw}mm;height:{ph}mm;overflow:hidden;}}
{css}
</style></head><body><div id="page" class="{cls}">{body}</div></body></html>"""


# --------------------------------------------------------------- 自动铺满
FIT_JS = """() => {
  const cols = document.querySelector('#cols');
  const l = document.querySelector('#colL .inner');
  const r = document.querySelector('#colR .inner');
  const avail = cols.clientHeight;
  const lh = l.getBoundingClientRect().height;
  const rh = r.getBoundingClientRect().height;
  return {need: Math.max(lh, rh), avail, lh, rh};
}"""


async def autofit(page):
    async def measure(fs):
        await page.evaluate("v => document.documentElement.style.setProperty('--fs', v)", f"{fs}mm")
        await page.wait_for_timeout(16)
        return await page.evaluate(FIT_JS)

    lo, hi = FS_MIN, FS_MAX
    best, best_m = FS_MIN, None
    for _ in range(16):
        mid = (lo + hi) / 2
        m = await measure(mid)
        if m["need"] <= m["avail"] - 1:
            best, best_m = mid, m
            lo = mid
        else:
            hi = mid
    await page.evaluate("v => document.documentElement.style.setProperty('--fs', v)", f"{best}mm")
    await page.wait_for_timeout(30)
    m = await page.evaluate(FIT_JS)
    return best, m


# --------------------------------------------------------------- 主流程
async def render(cfg, outdir, ratio, px, do_pdf, do_png, keep_html, flat=False):
    from playwright.async_api import async_playwright
    from PIL import Image

    pw, ph = RATIOS[ratio]
    html = build_html(cfg, pw, ph)
    if "{{" in html:
        print("[!] 模板残留占位符，请检查", file=sys.stderr)

    work = tempfile.mkdtemp(prefix="shenlun_")
    hp = os.path.join(work, "page.html")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(html)

    css_w, css_h = pw * PX_PER_MM, ph * PX_PER_MM

    # ---- 交付目录：一期一个文件夹，默认自动新建 ----
    folder = cfg.get("file_stem") or cfg["head"]["title"].replace(" ", "")
    fname = cfg.get("file_name") or folder
    target = outdir if flat else os.path.join(outdir, folder)
    os.makedirs(target, exist_ok=True)
    # 清掉上一轮残留的中间件，保证文件夹里只有交付物
    stale = os.path.join(target, fname + ".html")
    if os.path.exists(stale) and not keep_html:
        os.remove(stale)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": round(css_w), "height": round(css_h)},
            device_scale_factor=2.4)
        page = await ctx.new_page()
        await page.goto("file://" + hp)
        await page.wait_for_timeout(350)

        fs, m = await autofit(page)
        fill = m["need"] / m["avail"] if m["avail"] else 0
        print(f"[版式] {pw:.0f}×{ph:.0f}mm  基准字号 {fs:.2f}mm ≈ {fs/25.4*72:.1f}pt  "
              f"填充率 {fill*100:.0f}%  左栏 {m['lh']/m['avail']*100:.0f}% / 右栏 {m['rh']/m['avail']*100:.0f}%")
        if m["need"] > m["avail"] + 1:
            print(f"[!] 内容溢出 {m['need']-m['avail']:.0f}px，已压到最小字号仍放不下 —— 请删减文字", file=sys.stderr)
        elif fill < 0.88:
            print(f"[!] 填充率仅 {fill*100:.0f}%，字号已顶到 {FS_MAX}mm 上限 —— 内容偏少，建议补 3—5 行",
                  file=sys.stderr)

        outputs = []
        if do_png:
            shot = os.path.join(work, "shot.png")
            await page.screenshot(path=shot, clip={"x": 0, "y": 0,
                                                   "width": css_w, "height": css_h})
            img = Image.open(shot).convert("RGB")
            if px and img.width != px:
                img = img.resize((px, round(img.height * px / img.width)), Image.LANCZOS)
            png = os.path.join(target, fname + ".png")
            img.save(png, optimize=True)
            outputs.append(png)
            print(f"[PNG] {png}  {img.width}×{img.height}")

        if do_pdf:
            pdf = os.path.join(target, fname + ".pdf")
            await page.pdf(path=pdf, width=f"{pw}mm", height=f"{ph}mm",
                           margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                           print_background=True, page_ranges="1")
            outputs.append(pdf)
            print(f"[PDF] {pdf}")

        await browser.close()

    if keep_html:
        hp_out = os.path.join(target, fname + ".html")
        shutil.copy(hp, hp_out)
        outputs.append(hp_out)
    shutil.rmtree(work, ignore_errors=True)
    print(f"[交付] {target}")
    return fs, m, outputs


# --------------------------------------------------------------- 出厂自检
def check_brief(cfg):
    N = cfg["news"]["items"]
    M = (cfg.get("meetings") or {}).get("items", [])
    R = (cfg.get("region") or {}).get("items", [])
    G = (cfg.get("bottom", {}).get("gold") or {}).get("rows", [])
    F = (cfg.get("bottom", {}).get("figures") or {}).get("items", [])
    rows = []
    rows.append(("结构 · 时政条数", f"{len(N)} 条", 3 <= len(N) <= 6))
    miss = [it["no"] for it in N if not all(it.get(k) for k in ("dom", "title", "text", "point"))]
    rows.append(("结构 · 每条齐 dom/title/text/point", "齐" if not miss else f"缺 第{miss}条", not miss))
    long_t = [it["no"] for it in N if len(plain(it["title"])) > 24]
    rows.append(("结构 · 标题 ≤24 字", "合格" if not long_t else f"第{long_t}条超长", not long_t))
    rows.append(("结构 · 会议块", f"{len(M)} 个 / {sum(len(m['points']) for m in M)} 条要点",
                 len(M) >= 1 and all(len(m["points"]) >= 2 for m in M)))
    rows.append(("结构 · 广东·大湾区", f"{len(R)} 条", len(R) >= 3))
    rows.append(("结构 · 今日金句", f"{len(G)} 句", 1 <= len(G) <= 3))
    rows.append(("结构 · 数字速记", f"{len(F)} 个", 4 <= len(F) <= 8))
    no_ok = [it["no"] for it in N] == list(range(1, len(N) + 1))
    rows.append(("编号 · 时政 1..N 连续", "是" if no_ok else "否", no_ok))
    chars = sum(len(plain(it["text"])) for it in N)
    rows.append(("篇幅 · 时政摘要总字数", f"{chars} 字", 450 <= chars <= 1200))

    print("\n── 出厂自检（早报）──")
    allok = True
    for name, val, ok in rows:
        allok &= bool(ok)
        print(f"  {'✓' if ok else '✗'} {name}：{val}")
    print(f"  {'全部通过' if allok else '有未通过项，见上'}\n")
    return allok


def check(cfg):
    if cfg.get("template") == "brief":
        return check_brief(cfg)
    L = cfg["left"]["paras"]
    R = cfg["right"]
    left_txt = "".join(plain(p["text"]) for p in L).replace(" ", "")
    rows = []

    rows.append(("结构 · 原文段数", f"{len(L)} 段", len(L) >= 6))
    rows.append(("结构 · 拆解条数", f"{len(R['items'])} 条", len(R["items"]) == len(L)))
    bot = cfg.get("bottom", {})
    g = (bot.get("gold") or {}).get("rows", [])
    if g:
        rows.append(("结构 · 金句", f"{len(g)} 句", 2 <= len(g) <= 4))

    # 右栏金句必须真的出自左栏正文（原海报第 7 条就栽在这）
    bad = []
    for it in R["items"]:
        q = plain(it.get("quote", "")).replace(" ", "").replace("…", "")
        if not q:
            continue
        probe = q[:10]
        if probe and probe not in left_txt:
            bad.append(f"第{it['no']}条：{probe}…")
    rows.append(("一致性 · 右栏金句出自原文", "全部命中" if not bad else "未命中 " + "；".join(bad), not bad))

    # 金句带的句子同样要在正文里
    if g:
        bad2 = [r["text"][:10] for r in g
                if plain(r["text"])[:10].replace(" ", "") not in left_txt]
        rows.append(("一致性 · 金句带出自原文", "全部命中" if not bad2 else "未命中 " + "；".join(bad2), not bad2))

    # 编号连续
    ok_no = [p["no"] for p in L] == list(range(1, len(L) + 1))
    rows.append(("编号 · 段落 1..N 连续", "是" if ok_no else "否", ok_no))

    # 填空线
    blanks = sum(len(re.findall(r"_{2,}", r["v"]))
                 for r in (bot.get("patterns") or {}).get("rows", []))
    if bot.get("patterns"):
        rows.append(("模板 · 句式填空位数", f"{blanks} 处", blanks >= 3))

    chars = len(left_txt)
    rows.append(("篇幅 · 原文总字数", f"{chars} 字", 800 <= chars <= 2200))

    print("\n── 出厂自检 ──")
    allok = True
    for name, val, ok in rows:
        allok &= bool(ok)
        print(f"  {'✓' if ok else '✗'} {name}：{val}")
    print(f"  {'全部通过' if allok else '有未通过项，见上'}\n")
    return allok


def main():
    ap = argparse.ArgumentParser(description="申论日报一键排版")
    ap.add_argument("content")
    ap.add_argument("-o", "--outdir", default=".",
                    help="输出根目录，脚本会在其中自动新建一期文件夹")
    ap.add_argument("--ratio", default="2:3", choices=list(RATIOS))
    ap.add_argument("--px", type=int, default=1600,
                    help="长图输出宽度，默认 1600（高清）；0=不缩放；1080=小红书标准宽")
    ap.add_argument("--flat", action="store_true", help="不建子文件夹，直接输出到 -o 目录")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--keep-html", action="store_true")
    a = ap.parse_args()

    with open(a.content, encoding="utf-8") as f:
        cfg = json.load(f)
    check(cfg)
    asyncio.run(render(cfg, os.path.abspath(a.outdir), a.ratio, a.px,
                       not a.no_pdf, not a.no_png, a.keep_html, a.flat))


if __name__ == "__main__":
    main()
