"""Every account's artifacts in one place, without switching accounts.

An artifact's own link opens only for the account that owns it; a local copy opens in any browser, so a folder of
local copies is the one way to see all of them without signing out and in. This plugin does not make the copies:
it reads a folder you (or your own hook) fill, one subfolder per artifact:

    <artifacts>/<artifact id>/_meta.json   {"url": "<its link>",
                                            "publishes": [{"when": "20261021-150200", "files": ["index.html"],
                                                           "account": "~/.claude-work2", "description": "...",
                                                           "capabilities": {}, "type_url": null}]}
    <artifacts>/<artifact id>/index.html   (or the .html each publish names)

Folder: $MULTI_ACCOUNT_ARTIFACTS, else <data>/artifacts. An empty or missing folder simply lists nothing.
"""
import html as H
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

import accounts as A

ARTIFACTS = os.environ.get("MULTI_ACCOUNT_ARTIFACTS") or os.path.join(A.DATA, "artifacts")
GALLERY = os.path.join(A.DATA, "artifacts.html")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _when(text):
    try:
        return time.mktime(time.strptime(text, "%Y%m%d-%H%M%S"))
    except (TypeError, ValueError):
        return None


def _page(folder, pubs):
    """The page to open: the newest publish's .html that is on disk, else any index.html kept for it."""
    for p in reversed(pubs):
        for f in p.get("files") or []:
            if f.endswith(".html") and not os.path.isabs(f) and ".." not in f.split("/") \
                    and os.path.isfile(os.path.join(folder, f)):
                return os.path.join(folder, f)
    f = os.path.join(folder, "index.html")
    return f if os.path.isfile(f) else None


def _title(page):
    try:
        with open(page, encoding="utf-8", errors="replace") as f:
            m = TITLE_RE.search(f.read(200000))
    except OSError:
        return None
    t = H.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    return t or None


def listing(search=None, account=None):
    """Every artifact copy, newest first, with the account that published it."""
    by_dir = {A.short(c["dir"]): c for c in A.load()["accounts"]}
    out = []
    try:
        ids = os.listdir(ARTIFACTS)
    except OSError:
        return out
    for aid in ids:
        folder = os.path.join(ARTIFACTS, aid)
        if aid.startswith(("_", ".")):
            continue
        try:
            meta = json.load(open(os.path.join(folder, "_meta.json")))
        except (OSError, ValueError):
            continue
        pubs = meta.get("publishes") or []
        if not pubs:
            continue
        owner = A.short(pubs[0].get("account") or A.MAIN)
        r = by_dir.get(owner)
        page = _page(folder, pubs)
        newest = max((_when(p.get("when")) or 0 for p in pubs), default=0) or None
        desc = next((p.get("description") for p in reversed(pubs) if p.get("description")), None)
        out.append({
            "id": aid, "url": meta.get("url"),
            "title": (_title(page) if page else None) or desc or aid,
            "description": desc or "",
            "account": r["name"] if r else A.name_of(owner) if owner.startswith("~/.claude") else owner,
            "email": r.get("email") if r else None,
            "when": newest, "when_text": A.hour(newest) if newest else "-",
            "copy": page,
            "live": any(p.get("capabilities") for p in pubs),
            "type": next((p.get("type_url") for p in pubs if p.get("type_url")), None),
        })
    out.sort(key=lambda a: a["when"] or 0, reverse=True)
    if account:
        out = [a for a in out if a["account"] == account]
    if search:
        q = search.lower()
        out = [a for a in out if q in (a["title"] + " " + a["description"] + " " + a["id"]).lower()]
    return out


def summary(n=6):
    """What the pane shows: the newest few and how many each account has."""
    every = listing()
    per = {}
    for a in every:
        per[a["account"]] = per.get(a["account"], 0) + 1
    return {"total": len(every), "per_account": per, "newest": every[:n], "gallery": GALLERY, "folder": ARTIFACTS}


def open_in_browser(target):
    """Opens a file or link in the default browser (`open` on macOS, `xdg-open` elsewhere); MA_NO_OPEN only reports."""
    if os.environ.get("MA_NO_OPEN"):
        return False
    opener = "open" if sys.platform == "darwin" else shutil.which("xdg-open")
    if not opener:
        return False
    return subprocess.run([opener, target], capture_output=True).returncode == 0


def find_one(aid):
    for a in listing():
        if a["id"] == aid:
            return a
    raise ValueError("no artifact '%s' among the copies in %s" % (aid, ARTIFACTS))


def gallery():
    """One local page with every account's artifacts: search, a filter per account, the copy and the link."""
    every = listing()
    per = {}
    for a in every:
        per[a["account"]] = per.get(a["account"], 0) + 1
    data = [{"t": a["title"], "d": a["description"], "c": a["account"], "f": a["when_text"],
             "u": a["url"] or "", "v": "file://" + urllib.parse.quote(a["copy"]) if a["copy"] else "",
             "l": a["live"]} for a in every]
    js = json.dumps({"a": data, "p": per}, ensure_ascii=False).replace("</", "<\\/")
    os.makedirs(os.path.dirname(GALLERY), exist_ok=True)
    tmp = GALLERY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.replace("__DATA__", js).replace("__TIME__", A.hour(time.time())))
    os.replace(tmp, GALLERY)
    return GALLERY, len(every)


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Artifacts, all accounts</title>
<style>
:root{--bg:#f7f6f3;--card:#fff;--ink:#1f1e1c;--sub:#6b6862;--line:#e3e0da;--acc:#c2410c;--acc-ink:#fff;--chip:#efece6}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--card:#262522;--ink:#f1efe9;--sub:#a8a49c;--line:#3a3833;
--acc:#f97316;--acc-ink:#1c1b19;--chip:#302e2a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:28px;margin:0 0 4px}.sub{color:var(--sub);margin:0 0 20px}
.bar{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
input{flex:1 1 280px;min-height:44px;font:inherit;padding:8px 14px;border:1px solid var(--line);border-radius:10px;
background:var(--card);color:var(--ink)}
.chip{min-height:44px;padding:8px 16px;border-radius:999px;border:1px solid var(--line);background:var(--chip);
color:var(--ink);font:inherit;cursor:pointer}.chip[aria-pressed=true]{background:var(--acc);color:var(--acc-ink);border-color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;
flex-direction:column;gap:8px;min-height:190px}
.card h2{font-size:17px;margin:0;line-height:1.35}.meta{color:var(--sub);font-size:15px}
.desc{color:var(--sub);font-size:15px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.acc{display:flex;gap:8px;margin-top:auto}.acc a{flex:1;min-height:44px;display:flex;align-items:center;
justify-content:center;border-radius:10px;text-decoration:none;font-weight:600;border:1px solid var(--line);color:var(--ink)}
.acc a.view{background:var(--acc);color:var(--acc-ink);border-color:var(--acc)}.note{font-size:14px;color:var(--sub)}
.empty{color:var(--sub);padding:40px 0;text-align:center}
</style></head><body><main>
<h1>Artifacts from every account</h1>
<p class="sub">Local copies on this computer. <b>View here</b> opens the copy without switching accounts. <b>Open in Claude</b>
opens the original and needs the account that owns it. List built: __TIME__.</p>
<div class="bar"><input id="q" type="search" placeholder="Search by name or description" aria-label="Search">
<span id="chips" class="bar" style="margin:0"></span></div>
<div id="grid" class="grid"></div><p id="empty" class="empty" hidden>No artifacts match that search.</p>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
var D=JSON.parse(document.getElementById('data').textContent),flt='',q=document.getElementById('q');
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function chips(){var box=document.getElementById('chips');box.textContent='';
var accs=[['','All ('+D.a.length+')']].concat(Object.keys(D.p).map(function(k){return [k,k+' ('+D.p[k]+')']}));
accs.forEach(function(c){var b=el('button','chip',c[1]);b.setAttribute('aria-pressed',flt===c[0]);
b.onclick=function(){flt=c[0];chips();paint()};box.appendChild(b)})}
function paint(){var g=document.getElementById('grid'),t=q.value.toLowerCase(),n=0;g.textContent='';
D.a.forEach(function(a){if(flt&&a.c!==flt)return;if(t&&(a.t+' '+a.d).toLowerCase().indexOf(t)<0)return;n++;
var c=el('article','card');c.appendChild(el('h2','',a.t));c.appendChild(el('div','meta',a.c+' · '+a.f));
if(a.d)c.appendChild(el('div','desc',a.d));if(a.l&&a.v)c.appendChild(el('div','note','The copy has no live data.'));
var acc=el('div','acc');if(a.v){var v=el('a','view','View here');v.href=a.v;v.target='_blank';acc.appendChild(v)}
if(a.u){var o=el('a','','Open in Claude');o.href=a.u;o.target='_blank';acc.appendChild(o)}
c.appendChild(acc);g.appendChild(c)});document.getElementById('empty').hidden=n>0}
q.oninput=paint;chips();paint();
</script></body></html>
"""
