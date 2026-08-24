"""Generate static per-GPU answer pages (web/gpu/<slug>/) + directory + sitemap.
Numbers are baked at generation time and stamped; page JS re-fetches live on load.
Run: venv/bin/python gen_gpu_pages.py   (hourly timer: compute-gpu-pages.timer)"""
import json, re, time, urllib.request, pathlib, html

BASE = "http://127.0.0.1:8956"
SITE = "https://compute.wick.pics"
ROOT = pathlib.Path(__file__).parent / "web"
TOP_N = 20

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

def slugify(m):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", m.lower())).strip("-")

def fp(p):
    return "$" + (f"{p:.2f}" if p >= 1 else f"{p:.3f}")

_gpus_all = get("/api/gpus")["gpus"]
gpus = _gpus_all[:TOP_N]
gpu_by_model = {g["gpu_model"]: g for g in _gpus_all}
spread = {m["gpu_model"]: m for m in get("/api/spread")["models"]}
value = {}
for v in get("/api/value?limit=200").get("value", []):
    value.setdefault(v["gpu_model"], v)  # best class first (sorted desc)
_specs = get("/api/specs")
bw_map = {k.replace(" ", ""): v for k, v in _specs.get("mem_bw_gbs", {}).items()}
fp16_map = {k.replace(" ", ""): v for k, v in _specs.get("fp16_dense_tflops", {}).items()}
nfeeds = len(get("/api/stats").get("providers", {}))

now = time.gmtime()
stamp = time.strftime("%Y-%m-%d %H:%M UTC", now)
lastmod = time.strftime("%Y-%m-%d", now)

CSS = """
@font-face{font-family:'Erode';src:url('/fonts/erode-400.woff2') format('woff2');font-weight:400;font-display:swap}
@font-face{font-family:'Erode';src:url('/fonts/erode-600.woff2') format('woff2');font-weight:600;font-display:swap}
@font-face{font-family:'Martian Mono';src:url('/fonts/martian-mono-var.woff2') format('woff2');font-weight:100 800;font-display:swap}
:root{--stock:#0C110E;--ink:#E8EDE9;--ink2:#A8B3AA;--hair:rgba(232,237,233,.14);--amber:#F0A868;--phos:#7FD99A;
--serif:'Erode',Georgia,serif;--mono:'Martian Mono',ui-monospace,monospace;--sans:system-ui,-apple-system,'Segoe UI',sans-serif}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--stock);color:var(--ink);font-family:var(--sans);line-height:1.6;overflow-x:clip}
body::after{content:"";position:fixed;inset:0;z-index:60;pointer-events:none;opacity:.05;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}
.wrap{max-width:820px;margin:0 auto;padding:0 clamp(20px,5vw,48px)}
header{padding:26px 0}
.wordmark{font-family:var(--mono);font-weight:600;font-size:.95rem;text-decoration:none;color:var(--ink)}
.wordmark span{color:var(--ink2);font-weight:300}
a:focus-visible{outline:2px solid var(--amber);outline-offset:3px}
main{padding:clamp(30px,6vh,70px) 0 100px}
h1{font-family:var(--serif);font-weight:600;font-size:clamp(1.9rem,4.6vw,3rem);line-height:1.1;letter-spacing:-0.015em}
h1 em{font-style:normal;color:var(--amber)}
.asof{margin-top:12px;font-family:var(--mono);font-size:.7rem;color:var(--ink2)}
.asof .live{color:var(--phos)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0 28px;margin-top:34px;border-top:1px solid var(--hair)}
.stat{padding:18px 0;border-bottom:1px solid var(--hair)}
.stat .k{font-family:var(--mono);font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink2)}
.stat .v{margin-top:6px;font-family:var(--mono);font-size:1.35rem;font-weight:600}
.stat .v.phos{color:var(--phos)}
.stat .v small{font-size:.7rem;font-weight:400;color:var(--ink2)}
.verdict{margin-top:30px;font-family:var(--serif);font-size:1.2rem;line-height:1.5}
.verdict b{font-weight:600}
p.body{margin-top:18px;color:var(--ink2);max-width:62ch}
.ctas{margin-top:34px;display:flex;gap:14px;flex-wrap:wrap}
.btn{font-family:var(--mono);font-size:.82rem;font-weight:600;text-decoration:none;padding:12px 22px;border-radius:999px}
.btn-amber{background:var(--amber);color:#141007}
.btn-ghost{border:1px solid var(--hair);color:var(--ink)}
.api{margin-top:34px;font-family:var(--mono);font-size:.72rem;color:var(--ink2);line-height:2}
.api a{color:var(--ink)}
.mesh{margin-top:44px;padding-top:22px;border-top:1px solid var(--hair);font-family:var(--mono);font-size:.72rem;line-height:2.2;color:var(--ink2)}
.mesh a{color:var(--ink2);text-decoration:none;margin-right:14px;white-space:nowrap}
.mesh a:hover{color:var(--ink)}
footer{border-top:1px solid var(--hair);padding:30px 0 46px;font-family:var(--mono);font-size:.7rem;color:var(--ink2)}
footer a{color:var(--ink2)}
"""

def page(g, all_pairs):
    m = g["gpu_model"]; slug = slugify(m)
    sp = spread.get(m, {})
    od, it = sp.get("on_demand_min"), sp.get("interruptible_min")
    disc = sp.get("discount_pct")
    floor = g.get("min_price")
    vram = g.get("vram_gb")
    val = value.get(m)
    try:
        tim = get("/api/timing?gpu=" + urllib.request.quote(m))
    except Exception:
        tim = {}
    verdict = tim.get("verdict")
    cur = tim.get("current_best_per_gpu_hr")
    esc = html.escape(m)
    title = f"{esc} rental price — live GPU spot market"
    lowest = min([x for x in (od, it, floor) if x], default=None)
    desc = (f"Rent a {esc} from {fp(lowest)}/hr right now" if lowest else f"Live {esc} rental prices") + \
           f" — live prices from {nfeeds} GPU marketplaces, price history, and free under-price alerts. Zero platform fees."
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "Product", "name": f"{m} GPU rental (cloud, per hour)",
         "description": f"Hourly {m} rental offers aggregated live across GPU marketplaces, normalized to price per GPU-hour.",
         "brand": {"@type": "Brand", "name": "NVIDIA"},
         "offers": {"@type": "AggregateOffer", "priceCurrency": "USD",
                    **({"lowPrice": round(lowest, 4)} if lowest else {}),
                    "offerCount": g.get("offers", 0),
                    "url": f"{SITE}/gpu/{slug}/"}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Market", "item": SITE + "/"},
            {"@type": "ListItem", "position": 2, "name": "GPUs", "item": SITE + "/gpu/"},
            {"@type": "ListItem", "position": 3, "name": m, "item": f"{SITE}/gpu/{slug}/"}]}]}
    stats = []
    if it: stats.append(("best idle (interruptible)", fp(it) + "<small>/GPU/hr</small>", "phos"))
    if od: stats.append(("best on-demand", fp(od) + "<small>/GPU/hr</small>", ""))
    if disc: stats.append(("idle discount", f"−{disc:.0f}%", ""))
    stats.append(("live offers", str(g.get("offers", 0)), ""))
    if vram: stats.append(("vram · max listed", f"up to {vram:.0f} GB", ""))
    if val: stats.append(("FP16 TFLOPS·h per $", f"{val['tflops_per_usd_hr']:,.0f}", ""))
    bw = bw_map.get(m.replace(" ", ""))
    if bw and lowest: stats.append(("mem GB/s·h per $", f"{bw/lowest:,.0f}", ""))
    if vram and lowest: stats.append(("VRAM GB·h per $", f"{vram/lowest:,.0f}", ""))
    stat_html = "\n".join(
        f'<div class="stat"><div class="k">{k}</div><div class="v {c}" data-k="{k}">{v}</div></div>'
        for k, v, c in stats)
    verdict_html = ""
    if verdict and cur:
        verdict_html = (f'<p class="verdict">At {fp(cur)} per GPU-hour, the {esc} is '
                        f'<b>{html.escape(verdict)}</b>.</p>')
    mesh = " ".join(f'<a href="/gpu/{s}/">{html.escape(n)}</a>' for n, s in all_pairs if s != slug)
    qm = urllib.request.quote(m)
    return slug, f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{SITE}/gpu/{slug}/">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{SITE}/og.png">
<meta property="og:url" content="{SITE}/gpu/{slug}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="compute.wick.pics">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{json.dumps(ld, separators=(",", ":"))}</script>
<style>{CSS}</style>
</head>
<body>
<header><div class="wrap"><a class="wordmark" href="/">compute<span>.wick.pics</span></a></div></header>
<main class="wrap">
  <h1>{esc} rental, <em>live</em>.</h1>
  <p class="asof" id="asof">prices baked {stamp} · refreshing…</p>
  <div class="stats">{stat_html}</div>
  {verdict_html}
  <p class="body">Every number above comes from the live book: offers for this GPU across {nfeeds} rental marketplaces, normalized to dollars per GPU per hour so a one-card host and an eight-card cluster compare honestly. Interruptible rows are idle machines bidding for work — that is where the discount lives.</p>
  <div class="ctas">
    <a class="btn btn-amber" href="/#market">Open the live market</a>
    <a class="btn btn-ghost" href="/?watch={qm}#wire">Set a price tripwire</a>
    <a class="btn btn-ghost" href="/compare/?a={qm}">Compare</a>
    <a class="btn btn-ghost" href="/agents/">For agents</a>
  </div>
  <p class="api">Raw: <a href="/api/offers?gpu={qm}">offers</a> · <a href="/api/history?gpu={qm}">history</a> · <a href="/api/timing?gpu={qm}">timing</a> · <a href="/api/spread?gpu={qm}">spread</a> — JSON, no key.</p>
  <div class="mesh">Other GPUs: {mesh}</div>
</main>
<footer><div class="wrap">compute.wick.pics — the spot market for idle GPUs · zero platform fees · <a href="/faq/">fair questions</a> · <a href="/llms.txt">llms.txt</a></div></footer>
<script>
(function(){{
  var M={json.dumps(m)};
  Promise.all([
    fetch("/api/spread").then(r=>r.json()),
    fetch("/api/gpus").then(r=>r.json())
  ]).then(function(res){{
    var sp=(res[0].models||[]).find(function(x){{return x.gpu_model===M}});
    var g=(res[1].gpus||[]).find(function(x){{return x.gpu_model===M}});
    var set=function(k,v){{var el=document.querySelector('[data-k="'+k+'"]');if(el&&v)el.innerHTML=v}};
    var fp=function(p){{return "$"+(p>=1?p.toFixed(2):p.toFixed(3))}};
    if(sp){{
      if(sp.interruptible_min)set("best idle (interruptible)",fp(sp.interruptible_min)+"<small>/GPU/hr</small>");
      if(sp.on_demand_min)set("best on-demand",fp(sp.on_demand_min)+"<small>/GPU/hr</small>");
      if(sp.discount_pct)set("idle discount","−"+Math.round(sp.discount_pct)+"%");
    }}
    if(g)set("live offers",String(g.offers));
    document.getElementById("asof").innerHTML='<span class="live">live</span> · book read just now · baked copy was {stamp}';
  }}).catch(function(){{
    document.getElementById("asof").textContent="prices baked {stamp} (live refresh unavailable)";
  }});
}})();
</script>
</body>
</html>"""

pairs = [(g["gpu_model"], slugify(g["gpu_model"])) for g in gpus]
written = []
for g in gpus:
    slug, doc = page(g, pairs)
    d = ROOT / "gpu" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(doc)
    written.append(slug)

# ---------- vs pages: the classic comparisons, baked ----------
VS_PAIRS = [("H100 SXM", "A100 SXM4"), ("H200", "H100 SXM"), ("B200", "H200"),
            ("RTX 4090", "RTX 3090"), ("RTX 5090", "RTX 4090"),
            ("RTX 4090", "A100 PCIE"), ("L40S", "A100 PCIE"),
            ("A100 PCIE", "TESLA V100"), ("RTX A6000", "RTX 3090"),
            ("RTX 6000 ADA", "RTX A6000"), ("RTX 4090", "L40S"),
            ("RTX 5090", "H100 SXM")]

def _mrow(m):
    g = gpu_by_model.get(m, {})
    sp = spread.get(m, {})
    d = m.replace(" ", "")
    fp16 = fp16_map.get(d)
    mn = g.get("min_price")
    vram = g.get("vram_gb")
    return {"idle": sp.get("interruptible_min"), "od": sp.get("on_demand_min"),
            "disc": sp.get("discount_pct"), "offers": g.get("offers"),
            "vram": vram, "fp16": fp16, "bw": bw_map.get(d),
            "tpd": (fp16 / mn) if fp16 and mn else None,
            "vpd": (vram / mn) if vram and mn else None}

VS_ROWS = [("cheapest idle /GPU/hr", "idle", "low", fp),
           ("cheapest on-demand /GPU/hr", "od", "low", fp),
           ("idle discount", "disc", "high", lambda v: f"\u2212{v:.0f}%"),
           ("live offers", "offers", "high", lambda v: f"{v:.0f}"),
           ("vram, max listed", "vram", "high", lambda v: f"{v:.0f} GB"),
           ("fp16 spec TFLOPS", "fp16", "high", lambda v: f"{v:.0f}"),
           ("mem bandwidth GB/s", "bw", "high", lambda v: f"{v:,.0f}"),
           ("TFLOPS\u00b7h per $ (at floor)", "tpd", "high", lambda v: f"{v:,.0f}"),
           ("VRAM GB\u00b7h per $ (at floor)", "vpd", "high", lambda v: f"{v:,.0f}")]

VS_CSS = CSS + """
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.82rem;margin-top:30px;table-layout:fixed}
th{font-weight:600;text-align:left;padding:14px 12px 14px 0;border-bottom:1px solid var(--hair)}
th:first-child{font-weight:400;color:var(--ink2);font-size:.64rem;letter-spacing:.08em;text-transform:uppercase}
td{padding:11px 12px 11px 0;border-top:1px solid var(--hair);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.lbl{color:var(--ink2);font-size:.66rem;letter-spacing:.05em;text-transform:uppercase;white-space:normal}
td.win{color:var(--amber);font-weight:600}
td.thin{color:var(--ink2)}
.verdict{margin-top:22px;font-family:var(--serif);font-size:1.1rem;max-width:60ch}
"""

def vs_page(a, b):
    ra, rb = _mrow(a), _mrow(b)
    slug = f"{slugify(a)}-vs-{slugify(b)}"
    ea, eb = html.escape(a), html.escape(b)
    trs = []
    for label, k, direction, show in VS_ROWS:
        va, vb = ra[k], rb[k]
        if va is None and vb is None:
            continue
        def cell(v, other):
            if v is None:
                return '<td class="thin" title="unrated or not listed \u2014 never guessed">\u2014</td>'
            win = other is not None and ((v < other) if direction == "low" else (v > other))
            return f'<td class="{"win" if win else ""}">{show(v)}</td>'
        trs.append(f'<tr><td class="lbl">{label}</td>{cell(va, vb)}{cell(vb, va)}</tr>')
    verdict = ""
    if ra["tpd"] and rb["tpd"]:
        lead, hi, lo = (a, ra["tpd"], rb["tpd"]) if ra["tpd"] >= rb["tpd"] else (b, rb["tpd"], ra["tpd"])
        verdict = (f'<p class="verdict">At today\u2019s floors the <b>{html.escape(lead)}</b> delivers '
                   f'{hi:,.0f} FP16 TFLOPS\u00b7hours per dollar against {lo:,.0f} \u2014 '
                   f'{hi / lo:.1f}\u00d7 the training compute per dollar. Spec ceilings, not benchmarks.</p>')
    qa, qb = urllib.request.quote(a), urllib.request.quote(b)
    title = f"{ea} vs {eb} \u2014 GPU rental price & specs compared"
    desc = (f"{ea} vs {eb}: live rental floor prices, idle discounts, VRAM, spec throughput, "
            f"bandwidth and value per dollar, side by side across {nfeeds} marketplaces.")
    mesh = " ".join(
        f'<a href="/vs/{slugify(x)}-vs-{slugify(y)}/">{html.escape(x)} vs {html.escape(y)}</a>'
        for x, y in VS_PAIRS if (x, y) != (a, b))
    return slug, f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{SITE}/vs/{slug}/">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:image" content="{SITE}/og.png">
<meta property="og:url" content="{SITE}/vs/{slug}/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="compute.wick.pics">
<meta name="twitter:card" content="summary_large_image">
<style>{VS_CSS}</style>
</head>
<body>
<header><div class="wrap"><a class="wordmark" href="/">compute<span>.wick.pics</span></a></div></header>
<main class="wrap">
  <h1>{ea} <em>vs</em> {eb}.</h1>
  <p class="asof">prices baked {stamp} \u00b7 <a href="/compare/?a={qa}&amp;b={qb}">live, always-current version of this comparison \u2192</a></p>
  <table>
    <thead><tr><th>vs</th><th><a href="/gpu/{slugify(a)}/">{ea}</a></th><th><a href="/gpu/{slugify(b)}/">{eb}</a></th></tr></thead>
    <tbody>{"".join(trs)}</tbody>
  </table>
  {verdict}
  <p class="body">The amber cell wins its row \u2014 lowest on price, highest on everything else. Prices are each model\u2019s cheapest live listing across {nfeeds} marketplaces at bake time; idle prices are reclaimable bid-market floors. Spec figures are vendor ceilings, unrated cells say so.</p>
  <div class="ctas">
    <a class="btn btn-amber" href="/compare/?a={qa}&amp;b={qb}">Open the live comparison</a>
    <a class="btn btn-ghost" href="/#market">The whole market</a>
  </div>
  <div class="mesh">Other classics: {mesh}</div>
</main>
<footer><div class="wrap">compute.wick.pics \u2014 the spot market for idle GPUs \u00b7 <a href="/faq/">fair questions</a> \u00b7 <a href="/llms.txt">llms.txt</a></div></footer>
</body>
</html>
"""

vs_written = []
for a, b in VS_PAIRS:
    if a not in gpu_by_model or b not in gpu_by_model:
        continue
    slug, doc = vs_page(a, b)
    d = ROOT / "vs" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(doc)
    vs_written.append(slug)

# directory page
vs_mesh = " ".join(
    f'<a href="/vs/{slugify(a)}-vs-{slugify(b)}/">{html.escape(a)} vs {html.escape(b)}</a>'
    for a, b in VS_PAIRS if a in gpu_by_model and b in gpu_by_model)
items = "\n".join(
    f'<li><a href="/gpu/{slugify(g["gpu_model"])}/">{html.escape(g["gpu_model"])}</a>'
    f'<span>{("from " + fp(g["min_price"])) if g.get("min_price") else ""} · {g["offers"]} offers</span></li>'
    for g in gpus)
(ROOT / "gpu" / "index.html").write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU rental prices by model — compute.wick.pics</title>
<meta name="description" content="Live rental price pages for every major GPU model — best idle and on-demand price, history, and free price alerts across {nfeeds} marketplaces.">
<link rel="canonical" href="{SITE}/gpu/">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="GPU rental prices by model">
<meta property="og:description" content="Live rental price pages for every major GPU model. Zero platform fees.">
<meta property="og:image" content="{SITE}/og.png">
<meta property="og:url" content="{SITE}/gpu/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="compute.wick.pics">
<meta name="twitter:card" content="summary_large_image">
<style>{CSS}
ul{{list-style:none;margin-top:30px;border-top:1px solid var(--hair)}}
li{{border-bottom:1px solid var(--hair)}}
li a{{font-family:var(--mono);font-weight:600;color:var(--ink);text-decoration:none;display:inline-block;padding:14px 0}}
li a:hover{{color:var(--amber)}}
li span{{float:right;font-family:var(--mono);font-size:.72rem;color:var(--ink2);padding:16px 0}}</style>
</head>
<body>
<header><div class="wrap"><a class="wordmark" href="/">compute<span>.wick.pics</span></a></div></header>
<main class="wrap">
  <h1>GPU rental prices, <em>by model</em>.</h1>
  <p class="asof">top {len(gpus)} models by live offer count · baked {stamp} · the <a href="/" style="color:var(--ink)">market table</a> is always live</p>
  <ul>{items}</ul>
  <h2 style="font-family:var(--serif);font-weight:600;font-size:1.4rem;margin-top:54px">Classic comparisons.</h2>
  <div class="mesh" style="margin-top:14px">{vs_mesh}</div>
</main>
<footer><div class="wrap">compute.wick.pics — the spot market for idle GPUs · zero platform fees</div></footer>
</body>
</html>""")

# sitemap: statics + gpu pages


urls = [("/", "hourly"), ("/idle/", "hourly"), ("/agents/", "weekly"), ("/faq/", "weekly"), ("/status/", "hourly"), ("/receipts/", "daily"), ("/wire/", "hourly"), ("/spot/", "hourly"), ("/calc/", "daily"), ("/compare/", "daily"), ("/comp/", "weekly"),
        ("/eco/", "weekly"), ("/biz/", "weekly"), ("/gpu/", "daily")] + \
       [(f"/gpu/{s}/", "daily") for s in written] + \
       [(f"/vs/{s}/", "daily") for s in vs_written]
sm = ['<?xml version="1.0" encoding="UTF-8"?>',
      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
for u, f in urls:
    sm.append(f"  <url><loc>{SITE}{u}</loc><lastmod>{lastmod}</lastmod><changefreq>{f}</changefreq></url>")
sm.append("</urlset>")
(ROOT / "sitemap.xml").write_text("\n".join(sm) + "\n")
print("wrote", len(written), "gpu pages +", len(vs_written), "vs pages + directory + sitemap", f"({len(urls)} urls)")
