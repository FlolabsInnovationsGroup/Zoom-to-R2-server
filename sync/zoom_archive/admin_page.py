"""HTML + CSS of the admin page. Pure functions: plain data in, HTML string out (no I/O, no request objects).

render_admin(status=..., keys=..., log=..., groups=..., origin=..., nonce=...) and render_login(...) are
called from web.py. Plain CSS (system font, light/dark via prefers-color-scheme, tabular numbers, no external
requests, no inline style attributes: the CSP only allows the nonce'd <style>).

Colour tokens are semantic (surface, text, accent, ok, warn, bad). Contrast, computed:
light: text 17.0:1, muted 5.7:1 (on the page background and sunk areas, 6.0 on the card), accent 8.8:1, ok/warn/bad on their tints 5.6-7.2:1, switch edge 3.8:1.
dark: text 15.5:1, muted 6.9:1, accent 7.8:1, ok/warn/bad on their tints 7.8-10.3:1, switch edge 4.4:1.
"""
import re
from urllib.parse import quote

ADMIN_CSS = """
:root{color-scheme:light dark;
--bg:#f4f4f6;--card:#fff;--sunk:#eeeef2;--text:#1c1c1e;--muted:#5c5c66;--line:#e1e1e7;
--accent:#3d36b5;--on:#fff;--track:#dcdce4;--ctl:#6b6b75;
--ok:#17692f;--ok-bg:#e3f4e8;--warn:#7a4300;--warn-bg:#fff1d6;--warn-line:#e8b866;--bad:#b3261e;--bad-bg:#fde8e6;--bad-line:#e9a39d;
--shadow:0 1px 2px rgba(20,20,40,.06)}
@media (prefers-color-scheme:dark){:root{
--bg:#0f0f11;--card:#1a1a1d;--sunk:#131315;--text:#f2f2f4;--muted:#a2a2ab;--line:#2c2c31;
--accent:#a9a4ff;--on:#0f0f11;--track:#34343b;--ctl:#80808a;
--ok:#6fd68c;--ok-bg:#16301e;--warn:#ffcb70;--warn-bg:#2e230e;--warn-line:#6b5120;--bad:#ff9a92;--bad-bg:#3a1816;--bad-line:#74332e;
--shadow:none}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:.875rem/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-variant-numeric:tabular-nums;overflow-wrap:anywhere;-webkit-font-smoothing:antialiased}
header,main{max-width:1000px;margin:0 auto;padding-left:16px;padding-right:16px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding-top:20px;padding-bottom:4px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.mark{flex:none;width:28px;height:28px;border-radius:8px;background:var(--accent);color:var(--on);display:grid;place-items:center;font-weight:700;font-size:.9rem;letter-spacing:-.02em}
h1{font-size:1.125rem;line-height:1.25;letter-spacing:-.01em;margin:0}
h1 small{display:block;font-size:.75rem;font-weight:400;color:var(--muted);letter-spacing:0}
h2{font-size:1rem;line-height:1.3;letter-spacing:-.005em;margin:32px 0 10px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
h2 .aside{font-size:.75rem;font-weight:400;color:var(--muted);letter-spacing:0}
h3{font-size:.8125rem;font-weight:600;margin:0 0 6px;color:var(--muted)}
p{margin:0}
.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow)}
.card.quiet{padding:10px 14px;background:transparent;box-shadow:none}
.hero{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:12px;margin-bottom:10px}
.hero.solo{grid-template-columns:minmax(0,1fr)}
.hero .card{margin:0;padding:18px 20px}
@media (max-width:760px){.hero{grid-template-columns:minmax(0,1fr)}}
.eyebrow{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:.8125rem;font-weight:600;color:var(--muted);margin-bottom:2px}
.pct{font-size:2.75rem;line-height:1.05;font-weight:650;letter-spacing:-.03em}
.pct sup{font-size:1.25rem;font-weight:600;letter-spacing:0;margin-left:2px;vertical-align:baseline;position:relative;top:-.55em}
.sub{margin-top:2px}
.sub strong{font-weight:600}
progress{display:block;width:100%;height:12px;margin:12px 0 10px;border:0;border-radius:6px;overflow:hidden;appearance:none;-webkit-appearance:none;background:var(--track);color:var(--accent)}
progress::-webkit-progress-bar{background:var(--track);border-radius:6px}
progress::-webkit-progress-value{background:var(--accent);border-radius:6px}
progress::-moz-progress-bar{background:var(--accent);border-radius:6px}
progress.thin{height:6px;margin:10px 0 8px}
progress.ok::-webkit-progress-value{background:var(--ok)}progress.ok::-moz-progress-bar{background:var(--ok)}
progress.warn{background:var(--warn-line)}
progress.warn::-webkit-progress-bar{background:var(--warn-line)}
progress.warn::-webkit-progress-value{background:var(--warn)}progress.warn::-moz-progress-bar{background:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:6px 22px;margin:0;padding:0}
.stats div{min-width:0}
.stats dt{font-size:.75rem;color:var(--muted)}
.stats dd{margin:0;font-weight:600}
.trash{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn)}
.trash h3,.trash .eyebrow{color:inherit}
.trash .days{font-size:2.75rem;line-height:1.05;font-weight:650;letter-spacing:-.03em}
.trash .days small{font-size:1rem;font-weight:600;letter-spacing:0;margin-left:6px}
.trash.urgent{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}
.trash.urgent progress.warn{background:var(--bad-line)}
.trash.urgent progress.warn::-webkit-progress-bar{background:var(--bad-line)}
.trash.urgent progress.warn::-webkit-progress-value{background:var(--bad)}.trash.urgent progress.warn::-moz-progress-bar{background:var(--bad)}
.trash .action{margin-top:10px;padding-top:10px;border-top:1px solid var(--warn-line);color:inherit}
.trash.urgent .action{border-top-color:var(--bad-line)}
.trash.done{background:var(--card);border-color:var(--line);color:var(--text)}
.trash.done .eyebrow{color:var(--muted)}
.callout{border-radius:10px;padding:10px 14px;margin-bottom:10px;border:1px solid var(--line);background:var(--card)}
.callout.bad{background:var(--bad-bg);border-color:var(--bad-line);color:var(--bad)}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.75rem;color:var(--muted);font-weight:600;background:var(--sunk);white-space:nowrap}
tr:last-child>td{border-bottom:0}
td.empty{padding:18px 12px;color:var(--muted);text-align:center}
.num{text-align:right}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-bottom:10px;border-radius:12px}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.tag{display:inline-block;padding:0 8px;border-radius:999px;font-size:.75rem;line-height:1.5;font-weight:600;background:var(--sunk);color:var(--muted);white-space:nowrap}
.tag.ok{background:var(--ok-bg);color:var(--ok)}
.tag.bad{background:var(--bad-bg);color:var(--bad)}
.tag.warn{background:var(--warn-bg);color:var(--warn)}
.cap{text-transform:capitalize}
code,pre{font:.75rem/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{word-break:break-all}td.nowrap code{word-break:normal}
pre{background:var(--sunk);border:1px solid var(--line);padding:8px 10px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin:0;flex:1;min-width:0}
form.row{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
label,legend{font-size:.8125rem;font-weight:600}
.field{display:flex;flex-direction:column;gap:4px}
.field .hint{margin:0}
input[type=text],input[type=date],input[type=password]{font:inherit;min-height:36px;padding:5px 10px;border:1px solid var(--ctl);border-radius:8px;background:var(--card);color:var(--text);width:100%}
input::placeholder{color:var(--muted);opacity:1}
input[aria-invalid=true]{border-color:var(--bad)}
button{font:inherit;font-weight:500;min-height:32px;padding:4px 14px;border-radius:8px;border:1px solid var(--ctl);background:var(--card);color:var(--text);cursor:pointer;transition:background-color .12s,transform .12s}
button:hover{background:var(--sunk)}
button:active{transform:scale(.97)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on);font-weight:600}
button.primary:hover{filter:brightness(1.08)}
button.danger{color:var(--bad);border-color:var(--bad)}
button.danger:hover{background:var(--bad-bg)}
button.ghost{border-color:transparent;background:transparent;color:var(--muted)}
button.ghost:hover{background:var(--sunk);color:var(--text)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.hint{font-size:.75rem;color:var(--muted);margin:2px 0 0}
fieldset{border:0;margin:0;padding:0;min-width:0}
legend{padding:0;margin-bottom:2px}
.sw{display:flex;gap:10px;align-items:flex-start;min-height:28px;padding:3px 0;cursor:pointer;font-weight:400}
.sw .t{display:flex;flex-direction:column}
.sw .t b{font-weight:600;font-size:.8125rem}
.sw .t span{font-size:.75rem;color:var(--muted);line-height:1.35}
.sw input{appearance:none;-webkit-appearance:none;flex:none;width:34px;height:20px;margin:1px 0 0;border-radius:10px;background:var(--track);border:1px solid var(--ctl);position:relative;cursor:pointer;transition:background-color .15s,border-color .15s}
.sw input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--ctl);transition:transform .15s,background-color .15s}
.sw input:checked{background:var(--accent);border-color:var(--accent)}
.sw input:checked::after{transform:translateX(14px);background:var(--on)}
.sw.sm{padding:2px 0;align-items:center}
.sw.sm input{margin:0}
.sw.sm .t b{font-weight:500}
.switches{display:flex;flex-wrap:wrap;gap:0 16px}
.create{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:18px 28px;align-items:start}
.create .left{display:flex;flex-direction:column;gap:14px}
.create .actions{grid-column:1/-1;display:flex;align-items:center;gap:12px;border-top:1px solid var(--line);padding-top:14px}
@media (max-width:760px){.create{grid-template-columns:minmax(0,1fr)}}
.reveal{border-color:var(--ok);outline:0;padding:16px 18px;animation:rise .32s cubic-bezier(.2,.8,.2,1) both}
.reveal .head{display:flex;gap:12px;align-items:center;margin-bottom:12px}
.reveal h3{margin:0;color:var(--text);font-size:1rem}
.reveal .ico{flex:none;width:28px;height:28px;border-radius:50%;background:var(--ok-bg);color:var(--ok);display:grid;place-items:center;font-weight:700;animation:pop .4s cubic-bezier(.2,.9,.3,1.3) .08s both}
.once{display:inline-block;margin-top:2px;font-size:.8125rem;color:var(--warn);font-weight:600}
.copyrow{display:flex;gap:8px;align-items:flex-start;margin:4px 0 12px}
.copyrow:last-child{margin-bottom:0}
.copyrow button{flex:none}
.reveal .hint{margin:8px 0 0}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}
td .hint{margin:0}
a{color:var(--accent)}
.run{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;justify-content:space-between}
.run .state{font-size:1rem;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.run .state .tag{font-size:.875rem;padding:1px 10px}
.run .btns{display:flex;flex-wrap:wrap;gap:8px}
.run .btns form{margin:0}
details.section{margin:24px 0 10px}
details.section>summary{cursor:pointer;font-size:1rem;font-weight:600;padding:6px 0;display:flex;align-items:center;gap:8px;list-style:none}
details.section>summary::-webkit-details-marker{display:none}
details.section>summary::before{content:"›";display:inline-block;width:1em;color:var(--muted);transition:transform .15s}
details.section[open]>summary::before{transform:rotate(90deg)}
details.section>summary .aside{font-size:.75rem;font-weight:400;color:var(--muted)}
details.section>.scroll{margin-top:8px}
p.links{margin:14px 0 0}
form.filters{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;margin:12px 0}
form.filters .grow{flex:1;min-width:180px}
select{font:inherit;min-height:36px;padding:5px 30px 5px 10px;border:1px solid var(--ctl);border-radius:8px;background:var(--card);color:var(--text)}
@media (pointer:coarse){select{min-height:44px}}
.techlog{width:100%;margin-top:6px}.techlog summary{cursor:pointer;font-size:.75rem;color:var(--muted)}
.runlog{margin-top:8px;max-height:14rem;overflow:auto;flex:none;width:100%}
button:disabled,button:disabled:hover,button:disabled:active{background:var(--sunk);border-color:var(--line);color:var(--muted);opacity:1;cursor:not-allowed;transform:none;filter:none}
.sw:has(input:disabled){cursor:not-allowed;opacity:.6}
.filters{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 10px}
.filters span{color:var(--muted);font-size:.8125rem}
.filters a,.pager a{display:inline-block;padding:4px 12px;border-radius:999px}
.filters a{margin-left:4px}
.filters form{display:flex;gap:6px;align-items:center}
.filters a[aria-current]{color:var(--text);font-weight:600;text-decoration:none}
.pager{display:flex;gap:16px;justify-content:center;margin:4px 0 20px}
.nowrap{white-space:nowrap}
td[dir=auto]{overflow-wrap:break-word}
table.accounts{min-width:480px}
table.accounts td.host{overflow-wrap:break-word}
.login{max-width:380px;margin:0 auto;padding:12vh 16px 40px}
.login .brand{flex-direction:column;align-items:flex-start;gap:14px;margin-bottom:22px}
.login .mark{width:40px;height:40px;border-radius:11px;font-size:1.25rem}
.login h1{font-size:1.5rem;letter-spacing:-.02em}
.login h1 small{font-size:.875rem;margin-top:4px}
.login .card{padding:20px;display:flex;flex-direction:column;gap:16px}
.login button{min-height:40px;width:100%}
.login .foot{margin-top:14px;text-align:center}
@media (pointer:coarse){.filters a,.pager a{min-height:44px;display:inline-flex;align-items:center}button{min-height:44px}input[type=text],input[type=date],input[type=password]{min-height:44px}.sw{min-height:44px;align-items:center}}
@media (max-width:640px){
header,main{padding-left:12px;padding-right:12px}
.pct,.trash .days{font-size:2.25rem}
table.keys,table.keys tbody,table.keys tr,table.keys td{display:block;width:100%}
table.keys{background:transparent;border:0;box-shadow:none;border-radius:0}
table.keys tr:first-child{display:none}
table.keys tr{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:10px;padding:6px 0;box-shadow:var(--shadow)}
table.keys td{border:0;padding:4px 14px;display:flex;gap:12px;justify-content:space-between;align-items:flex-start;text-align:left}
table.keys td::before{content:attr(data-label);flex:none;width:78px;font-size:.75rem;color:var(--muted);padding-top:2px}
table.keys td>*{min-width:0}
table.recs,table.recs tbody,table.recs tr,table.recs td{display:block;width:100%}
table.recs{background:transparent;border:0;box-shadow:none;border-radius:0}
table.recs tr:first-child{display:none}
table.recs tr{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:10px;padding:6px 0;box-shadow:var(--shadow)}
table.recs td{border:0;padding:2px 14px;display:flex;gap:12px;justify-content:space-between;text-align:left}
table.recs td::before{content:attr(data-label);flex:none;width:64px;font-size:.75rem;color:var(--muted);padding-top:2px}
table.recs td.empty{display:block;text-align:center}
table.recs td.empty::before{content:none}
table.keys td.empty{display:block;text-align:center}
table.keys td.empty::before{content:none}
table.keys td[data-label=Access]{flex-direction:column;gap:2px}
table.keys td[data-label=Access]::before{width:auto}
table.keys td[data-label=Access] form{width:100%}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media (prefers-contrast:more){:root{--line:#767680;--muted:#3a3a42}.card,table{border-color:var(--ctl)}}
@media (prefers-contrast:more) and (prefers-color-scheme:dark){:root{--line:#9a9aa4;--muted:#d0d0d6}}
@media (forced-colors:active){.sw input{border:2px solid ButtonText}.sw input:checked{background:Highlight}progress{border:1px solid ButtonText}}
"""

SCRIPT = """
document.addEventListener("click",e=>{const b=e.target.closest("[data-copy]");if(!b)return;
const n=document.getElementById(b.dataset.copy),l=b.dataset.label||(b.dataset.label=b.textContent);
const done=()=>{b.textContent="Copied";setTimeout(()=>{b.textContent=l},2000)};
(navigator.clipboard?navigator.clipboard.writeText(n.textContent):Promise.reject()).then(done,()=>{const r=document.createRange();r.selectNodeContents(n);const s=getSelection();s.removeAllRanges();s.addRange(r);b.textContent="Press Ctrl+C"})});
document.addEventListener("submit",e=>{const f=e.target,m=f.dataset.confirm;if(m&&!confirm(m)){e.preventDefault();return}
if(f.dataset.busy){const b=f.querySelector("button");setTimeout(()=>{b.disabled=true;b.textContent="Checking..."},0);
const h=document.createElement("div");h.className="hint";h.setAttribute("role","status");h.textContent=f.dataset.busy;f.after(h)}
if(f.hasAttribute("data-needs-group")){const bs=f.querySelectorAll("input[name=groups]");const ok=[...bs].some(b=>b.checked);
if(bs.length)bs[0].setCustomValidity(ok?"":"Choose at least one access group.");if(!ok){bs[0].reportValidity();e.preventDefault()}}});
document.addEventListener("change",e=>{if(e.target.name==="groups")e.target.form.querySelectorAll("input[name=groups]").forEach(b=>b.setCustomValidity(""))});
document.addEventListener("change",e=>{if(e.target.matches("select[data-autosubmit]"))e.target.form.submit()});
let live=!!document.body.dataset.live;
setInterval(()=>{if(!live)return;fetch(location.pathname,{credentials:"same-origin"}).then(r=>r.ok?r.text():null).then(t=>{if(!t)return;
const d=new DOMParser().parseFromString(t,"text/html"),n=d.getElementById("sync"),o=document.getElementById("sync");if(!n||!o)return;
const open=!!o.querySelector("details[open]");o.innerHTML=n.innerHTML;if(open){const x=o.querySelector("details");if(x)x.open=true}
live=d.body.dataset.live!==undefined}).catch(()=>{})},5000);
const c=document.getElementById("created");if(c){c.scrollIntoView({block:"nearest"});c.focus({preventScroll:true})}
"""


def esc(v) -> str:
    return "".join(f"&#{ord(c)};" if c in "&<>\"'" else c for c in ("" if v is None else str(v)))


def gb(n) -> str:
    return f"{(n or 0) / 1e9:,.1f} GB"


def num(n) -> str:
    return f"{int(n or 0):,}"


def when(iso) -> str:
    """2026-11-09T10:00:00Z -> '2026-11-09 10:00 UTC'"""
    if not iso:
        return "never"
    return re.sub(r":\d\dZ$", " UTC", str(iso).replace("T", " "))


def csp(nonce: str) -> str:
    return (f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def _document(nonce: str, body: str, script: bool = True, title: str = "Admin - FloLabs Meetings Archive",
              live: bool = False) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f'<title>{esc(title)}</title><style nonce="{nonce}">{ADMIN_CSS}</style></head>'
            f'<body{" data-live" if live else ""}>{body}'
            + (f'<script nonce="{nonce}">{SCRIPT}</script>' if script else "") + "</body></html>")


def _brand(sub: str) -> str:
    return (f'<div class="brand"><div class="mark" aria-hidden="true">F</div>'
            f'<h1>FloLabs Meetings Archive<small>{esc(sub)}</small></h1></div>')


def render_login(nonce: str, error: str = "") -> str:
    err = f'<div class="callout bad" role="alert" id="e">{esc(error)}</div>' if error else ""
    invalid = ' aria-invalid="true" aria-describedby="e"' if error else ""
    return _document(nonce, f"""<main class="login">{_brand("Admin sign-in")}
<form method="post" action="/admin/login" class="card">{err}
<div class="field"><label for="pw">Password</label>
<input id="pw" type="password" name="password" required autofocus autocomplete="current-password"{invalid}></div>
<button class="primary">Sign in</button></form>
<p class="hint foot">Internal tool for the FloLabs team.</p></main>""", script=False,
                     title="Sign in - FloLabs Meetings Archive")


def _skipped(t: dict) -> str:
    n = t.get("skipped_meetings") or 0
    return (f'<p class="hint">Not copied: {num(n)} meetings whose transcript has under 300 words or no transcript '
            f'({gb(t.get("skipped_bytes"))}). <a href="/admin/recordings?status=skipped">See them</a></p>') if n else ""


def duration(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{max(m, 1)} min"


def _failed_tag(n) -> str:
    return f'<span class="tag bad">! {n} failed</span>' if n else "0 failed"


def _backfill(t: dict) -> str:
    total_b, done_b = t["bytes_total"] or 0, t["bytes_verified"] or 0
    files, ver = t["files"] or 0, t["verified"] or 0
    frac = (done_b / total_b) if total_b else ((ver / files) if files else 0)
    complete = bool(files) and ver >= files and not t["failed"]
    pct = 100 if complete else min(99, int(frac * 100))
    cls = ' class="ok"' if complete else ""
    state = '<span class="tag ok">Complete</span>' if complete else ""
    return f"""<section class="card" aria-labelledby="bf"><div class="eyebrow"><span id="bf">Copied from Zoom to R2</span>{state}</div>
<div class="pct">{pct}<sup>%</sup></div>
<div class="sub"><strong>{gb(done_b)}</strong> <span class="muted">of {gb(total_b)} copied</span></div>
<progress{cls} value="{done_b if total_b else ver}" max="{total_b or files or 1}" aria-label="Copy progress, {pct} percent">{pct}%</progress>
<dl class="stats"><div><dt>Videos</dt><dd>{num(t.get("videos_done", 0))} of {num(t.get("videos", 0))}</dd></div>
<div><dt>Meetings</dt><dd>{num(t["meetings_complete"])} of {num(t["meetings"])}</dd></div>
<div><dt>All files</dt><dd>{num(ver)} of {num(files)}</dd></div>
<div><dt>Failed</dt><dd>{_failed_tag(t["failed"])}</dd></div></dl>{_skipped(t)}</section>"""


def _trash(tr: dict) -> str:
    n, a = tr["meetings"], tr["archived"]
    if a >= n:
        return (f'<section class="card trash done" aria-labelledby="tz"><div class="eyebrow"><span id="tz">Zoom Trash</span>'
                f'<span class="tag ok">All archived</span></div><div class="sub"><strong>{num(n)} meetings</strong> '
                f'<span class="muted">({gb(tr["bytes"])}) are archived.</span></div></section>')
    dl = tr.get("days_left")
    urgent = dl is not None and dl <= 7
    big = when_ = ""
    if tr.get("purge_date"):
        if dl is not None:
            big = f'<div class="days">{dl}<small>{"day" if dl == 1 else "days"} left</small></div>'
        when_ = f'<div class="sub">Purged on <strong>{esc(tr["purge_date"])}</strong></div>'
    return f"""<section class="card trash{" urgent" if urgent else ""}" aria-labelledby="tz"><div class="eyebrow"><span id="tz">Zoom Trash</span></div>
{big}{when_}
<progress class="warn thin" value="{a}" max="{n or 1}" aria-label="Trash meetings archived">{a} of {n}</progress>
<div><strong>{num(a)} of {num(n)}</strong> meetings archived &middot; {gb(tr["bytes"])}</div>
<div class="action"><strong>John must click Recover in the Zoom portal</strong> so the rest can be downloaded before the purge.</div></section>"""


def _run_card(run, run_log: list, totals: dict | None = None) -> str:
    """Start / scan / stop buttons, the run state and the last lines of sync.log."""
    t = totals or {}
    found = f'{num(t.get("meetings", 0))} meetings, {num(t.get("videos", 0))} videos, {gb(t.get("bytes_total", 0))}'
    left_b = max((t.get("bytes_total") or 0) - (t.get("bytes_verified") or 0), 0)
    speed = t.get("speed_bps") or 0
    if not run:
        state = ('<span class="tag">Idle</span> Nothing counted yet. Press Scan Zoom to count every recording.'
                 if not t.get("files") else
                 f'<span class="tag ok">Done</span> Everything in Zoom is copied ({found}).' if not left_b else
                 f'<span class="tag">Paused</span> Zoom has {found}. {gb(left_b)} still to copy.')
    elif run.get("stopping"):
        state = '<span class="tag warn">Stopping</span> Finishing up. Unfinished files stay queued for next time.'
    elif run.get("mode") == "scan":
        state = f'<span class="tag ok">Counting</span> Reading every recording in Zoom: {found} found so far.'
    else:
        eta = f", about {duration(left_b / speed)} left" if speed else ", speed shows once the first files finish"
        rate = f" at {speed / 1e6:,.1f} MB/s" if speed else ""
        state = f'<span class="tag ok">Copying</span> {gb(left_b)} to go{rate}{eta}.'
    start_label = ("Start copying" if not t.get("verified") else
                   "Resume copying" if t.get("files", 0) > t.get("verified", 0) else "Re-check copies")
    off = " disabled" if run else ""
    btn = lambda action, label, cls="": (  # noqa: E731
        f'<form method="post"><input type="hidden" name="action" value="{action}">'
        f'<button class="{cls}"{off}>{label}</button></form>')
    mode = esc((run or {}).get("mode") or "run")
    stop = (f'<form method="post" data-confirm="Stop the {mode}? Downloads in progress are cancelled; unfinished files stay pending and Start resumes them.">'
            f'<input type="hidden" name="action" value="stop"><button class="danger"'
            f'{" disabled" if not run or run.get("stopping") else ""}>{"Stopping..." if run and run.get("stopping") else "Stop"}</button></form>')
    tail = (f'<details class="techlog"><summary>Technical log</summary><pre class="runlog">{esc(chr(10).join(run_log))}</pre></details>'
            if run_log else "")
    hint = ("Stop the current run to start another." if run else
            "Copying puts every recording not yet in R2 there, newest first. Stop any time; it picks up where it left off. "
            "Scan Zoom only counts recordings (1-2 minutes) and copies nothing.")
    return (f'<section class="card" aria-labelledby="rc"><div class="run"><div><h3 id="rc">Zoom to R2</h3>'
            f'<div class="state" role="status" aria-live="polite">{state}</div><div class="hint">{hint}</div></div>'
            f'<div class="btns">{btn("start_backfill", start_label, "primary")}{btn("start_scan", "Scan Zoom")}{stop}</div>'
            f'{tail}</div></section>')


def _cloud_card(lock: dict, totals: dict | None = None) -> str:
    """Serverless (Vercel): no long-running copy. Zoom is checked once a day by a cron, or now with the button.
    lock: the sync lock object ({"running", "since"} or {"running": False, "last": summary})."""
    t = totals or {}
    left_b = max((t.get("bytes_total") or 0) - (t.get("bytes_verified") or 0), 0)
    if lock.get("running"):
        state = '<span class="tag ok">Checking Zoom</span> New recordings are being copied to R2 now.'
    elif not t.get("files"):
        state = '<span class="tag">Idle</span> Nothing synced yet.'
    else:
        state = (f'<span class="tag ok">Up to date</span> Everything found in Zoom is in R2.' if not left_b else
                 f'<span class="tag">Waiting</span> {gb(left_b)} still to copy on the next check.')
    last = lock.get("last") or {}
    if last.get("finished"):
        what = (f'failed: {esc(last["error"])}' if last.get("error") else
                f'copied {num(last.get("files", 0))} files ({gb(last.get("bytes", 0))})'
                + (f', {num(last["failed"])} failed' if last.get("failed") else "")
                + (f', {num(last["left"])} left for next time' if last.get("left") else ""))
        by = "the daily check" if last.get("by") == "cron" else "Check Zoom now"
        state += f'<div class="hint">Last check {esc(when(last["finished"]))} by {by}: {what}.</div>'
    off = " disabled" if lock.get("running") else ""
    btn = (f'<form method="post" data-busy="Checking Zoom... this can take a few minutes. Keep this page open.">'
           f'<input type="hidden" name="action" value="sync_now"><button class="primary"{off}>Check Zoom now</button></form>')
    hint = ("Zoom is checked automatically once a day. New recordings are copied to R2, newest first; "
            "anything not finished in one check is picked up by the next.")
    return (f'<section class="card" aria-labelledby="rc"><div class="run"><div><h3 id="rc">Zoom to R2</h3>'
            f'<div class="state" role="status" aria-live="polite">{state}</div><div class="hint">{hint}</div></div>'
            f'<div class="btns">{btn}</div></div></section>')


def _status(s) -> str:
    """s: the dict from outputs.status_json (or None before the first sync run)."""
    if not s:
        return ('<div class="card muted"><strong>The sync has not run yet.</strong> Status appears here after its first run.'
                '<div class="hint">Reload this page once it has finished.</div></div>')
    t, r, tr = s["totals"], s.get("last_run"), s.get("trash")
    show_trash = bool(tr and tr["meetings"] > 0)
    if r:
        ran = esc(when(r["finished_at"])) if r.get("finished_at") else "running now"
        code = esc(r["exit_code"] if r["exit_code"] is not None else "-")
        last = (f'<strong>Last run</strong> {ran} &middot; {esc(r["mode"])} &middot; {num(r["files_done"])} files, '
                f'{_failed_tag(r["files_failed"])} &middot; exit {code}')
    else:
        last = "<strong>Last run</strong> none recorded"
    hero = f'<div class="hero{"" if show_trash else " solo"}">{_backfill(t)}{_trash(tr) if show_trash else ""}</div>'
    lastcard = f'<div class="card quiet">{last}<div class="hint">Status as of {esc(when(s["generated_at"]))}</div></div>'
    failed = ""
    if s.get("failed_files"):
        rows = "".join(
            f'<tr><td dir="auto">{(esc(f["topic"]) + "<br>") if f.get("topic") else ""}<code>{esc(f["meeting_key"])}</code></td>'
            f'<td>{esc(f["recording_type"])}</td><td dir="auto">{esc(f["last_error"])}</td></tr>' for f in s["failed_files"])
        failed = (f'<details class="section"><summary>Failed files <span class="tag bad">{len(s["failed_files"])}</span></summary>'
                  '<div class="scroll"><table><caption class="sr-only">Failed files</caption><tr><th scope="col">Meeting</th>'
                  f'<th scope="col">Type</th><th scope="col">Error</th></tr>{rows}</table></div></details>')
    unmapped_tag = '<span class="tag bad">! not mapped: no key group covers it</span>'
    def archived(a):
        done, total = a.get("meetings_complete"), a["bytes"] or 0
        if done is None:
            return ""
        pct = int(100 * (a.get("bytes_verified") or 0) / total) if total else 0
        return f'{num(done)} of {num(a["meetings"])} meetings ({pct}% of bytes)'
    rows = "".join(
        f'<tr><td class="nowrap"><code>{esc(a["account"])}-account/</code></td><td class="host">{esc(a["host_email"])}</td>'
        f'<td class="num">{num(a["meetings"])}</td><td class="num">{archived(a)}</td>'
        f'<td class="num nowrap">{gb(a["bytes"])}</td><td>{"" if a["mapped"] else unmapped_tag}</td></tr>'
        for a in s["accounts"])
    accounts = ('<p class="links"><a href="/admin/recordings">All recordings</a></p>'
                '<details class="section"><summary>Accounts</summary>'
                '<div class="scroll"><table class="accounts"><caption class="sr-only">Accounts</caption>'
                '<tr><th scope="col">Folder</th><th scope="col">Host</th>'
                '<th scope="col" class="num">Meetings</th><th scope="col" class="num">Archived</th>'
                '<th scope="col" class="num">Size</th>'
                f'<th scope="col"><span class="sr-only">Mapping</span></th></tr>{rows}</table></div></details>')
    return f"{hero}{lastcard}{failed}{accounts}"


def _boxes(groups: dict, checked: list, small: bool = False, off: bool = False) -> str:
    """Access groups as switches. The compact form (key list) keeps the description as a tooltip."""
    out = []
    for i, g in groups.items():
        on = (" checked" if i in checked else "") + (" disabled" if off else "")
        if small:
            out.append(f'<label class="sw sm" title="{esc(g["description"])}"><input type="checkbox" role="switch" '
                       f'name="groups" value="{esc(i)}"{on}><span class="t"><b>{esc(g["label"])}</b></span></label>')
        else:
            out.append(f'<label class="sw"><input type="checkbox" role="switch" name="groups" value="{esc(i)}"{on}>'
                       f'<span class="t"><b>{esc(g["label"])}</b><span>{esc(g["description"])}</span></span></label>')
    return "".join(out)


def _key_row(k: dict, groups: dict) -> str:
    st = k["state"]
    cls = {"active": "ok", "expired": "warn"}.get(st, "")
    exp = f'<div class="hint">expires {esc(when(k["expires_at"]))}</div>' if k["expires_at"] else ""
    revoke = ("" if st == "revoked" else
              f'<form method="post" data-confirm="Revoke {esc(k["name"])}? Connections using it stop working.">'
              f'<input type="hidden" name="action" value="revoke"><input type="hidden" name="id" value="{esc(k["id"])}">'
              '<button class="danger">Revoke</button></form>')
    return (f'<tr><td data-label="Name"><strong dir="auto">{esc(k["name"])}</strong></td>'
            f'<td data-label="Prefix" class="nowrap"><code>{esc(k["key_prefix"])}</code></td>'
            f'<td data-label="State"><span class="tag cap {cls}">{esc(st)}</span>{exp}</td>'
            '<td data-label="Access"><form method="post" class="row"><input type="hidden" name="action" value="groups">'
            f'<input type="hidden" name="id" value="{esc(k["id"])}"><span class="switches">{_boxes(groups, k["groups"], True, st == "revoked")}</span>'
            f'<button{" disabled" if st == "revoked" else ""}>Save access</button></form></td>'
            f'<td data-label="Last used" class="nowrap">{esc(when(k["last_used_at"]))}</td><td data-label="Actions">{revoke}</td></tr>')


def _reveal(origin: str, new_key: str) -> str:
    url = f"{origin}/mcp/{new_key}"
    cmd = f"claude mcp add --transport http flolabs-meetings {url}"
    return f"""<section class="card reveal" id="created" tabindex="-1" aria-labelledby="ch" role="status">
<div class="head"><div class="ico" aria-hidden="true">&#10003;</div><div><h3 id="ch">Key created</h3>
<span class="once">Copy it now. It is shown only once.</span></div></div>
<p class="hint">Connector URL for Claude.ai and ChatGPT</p><div class="copyrow"><pre id="u">{esc(url)}</pre><button class="primary" data-copy="u">Copy URL</button></div>
<p class="hint">Claude Code command</p><div class="copyrow"><pre id="c">{esc(cmd)}</pre><button data-copy="c">Copy command</button></div>
<p class="hint">The key on its own</p><div class="copyrow"><pre id="k">{esc(new_key)}</pre><button data-copy="k">Copy key</button></div></section>"""


def render_admin(*, status, keys: list, log: list, groups: dict, origin: str, nonce: str,
                 new_key: str = "", error: str = "", run=None, run_log: list | None = None, cloud: dict | None = None) -> str:
    """status: dict or None; keys: dicts with id, name, key_prefix, state, expires_at, groups(list), last_used_at;
    log: dicts with at, tool, meeting_key, result, name; groups: {id: {label, description}};
    run: runctl.running() dict or None; run_log: last lines of sync.log."""
    created = _reveal(origin, new_key) if new_key else ""
    err = f'<div class="callout bad" role="alert">{esc(error)}</div>' if error else ""
    boxes = _boxes(groups, [])
    run_err = ""
    if error.startswith(("A run is already", "A sync is already")):  # a refused Start shows next to the buttons, not above the key form
        run_err, err = err, ""
    key_rows = "".join(_key_row(k, groups) for k in keys) or \
        '<tr><td colspan="6" class="empty">No keys yet. Create one above to connect Claude.</td></tr>'
    log_rows = "".join(
        f'<tr><td class="nowrap">{esc(when(l["at"]))}</td><td dir="auto">{esc(l["name"] or "(deleted)")}</td><td>{esc(l["tool"])}</td>'
        f'<td dir="auto"><code>{esc(l["meeting_key"] or "")}</code></td><td><span class="tag {"ok" if l["result"] == "ok" else "bad"}">'
        f'{"&#10003; " if l["result"] == "ok" else "&#10005; "}{esc(l["result"])}</span></td></tr>' for l in log) or \
        '<tr><td colspan="5" class="empty">No calls yet. Calls appear here once a key is used.</td></tr>'
    return _document(nonce, f"""<header>{_brand("Admin")}<form method="post" action="/admin/logout"><button class="ghost">Sign out</button></form></header><main>
<h2>Sync status</h2><div id="sync">{run_err}{_cloud_card(cloud, (status or {}).get("totals")) if cloud is not None else _run_card(run, run_log or [], (status or {}).get("totals"))}{_status(status) if status or not run else ""}</div>
<h2>API keys</h2>{created}{err}
<form method="post" class="card create" data-needs-group><input type="hidden" name="action" value="create">
<div class="left"><div class="field"><label for="kn">Name</label>
<input id="kn" type="text" name="name" required maxlength="100" placeholder="Ahsan - Claude Code" autocomplete="off">
<p class="hint">Who or what will use this key.</p></div>
<div class="field"><label for="ke">Expires (optional)</label><input id="ke" type="date" name="expires"><p class="hint">Leave empty for no expiry.</p></div></div>
<fieldset><legend>Access</legend><p class="hint">Pick at least one group. You can change this later.</p>{boxes}</fieldset>
<div class="actions"><button class="primary">Create key</button></div></form>
<div class="scroll"><table class="keys"><caption class="sr-only">API keys</caption><tr><th scope="col">Name</th><th scope="col">Prefix</th><th scope="col">State</th><th scope="col">Access</th><th scope="col">Last used</th><th scope="col"><span class="sr-only">Actions</span></th></tr>{key_rows}</table></div>
<details class="section"><summary>Recent access <span class="aside">last 50 calls</span></summary>
<div class="scroll"><table><caption class="sr-only">Recent access log</caption><tr><th scope="col">When</th><th scope="col">Key</th><th scope="col">Tool</th><th scope="col">Meeting</th><th scope="col">Result</th></tr>{log_rows}</table></div></details>
</main>""", live=bool(run) or bool((cloud or {}).get("running")))


STATE_TAGS = {"archived": ("ok", "Archived"), "pending": ("", "Pending"), "partial": ("warn", "Pending (partial)"),
              "failed": ("bad", "Failed"), "skipped": ("", "Not copied (short transcript)")}
ACCOUNT_LABELS = (("", "All"), ("pm", "PM"), ("john", "John"), ("buddy", "Buddy"))
ACCOUNT_NAMES = dict(ACCOUNT_LABELS)
STATUS_LABELS = (("", "All"), ("archived", "Archived"), ("pending", "Pending"), ("failed", "Failed"),
                 ("skipped", "Not copied"))


def render_recordings(*, meetings: list, shown: int, totals: dict, page: int, pages: int, account: str, status: str,
                      nonce: str, q: str = "", first: int = 1) -> str:
    """meetings: this page's dicts from web.meetings_of (start, account, team, topic, files, verified, state, bytes).
    shown = all meetings matching the filters, first = number of this page's first row."""
    def link(a, st, pg, label, cur=False):
        qs = "&".join(x for x in (f"account={a}" if a else "", f"status={st}" if st else "",
                                  f"q={quote(q)}" if q else "", f"page={pg}" if pg > 1 else "") if x)
        return f'<a href="/admin/recordings{"?" + qs if qs else ""}"{" aria-current=page" if cur else ""}>{esc(label)}</a>'
    def select(name, label, options, cur):
        opts = "".join(f'<option value="{esc(v)}"{" selected" if v == cur else ""}>{esc(lab)}</option>' for v, lab in options)
        return (f'<div class="field"><label for="f-{name}">{label}</label>'
                f'<select id="f-{name}" name="{name}" data-autosubmit>{opts}</select></div>')
    filters = ('<form method="get" action="/admin/recordings" class="filters">'
               + select("account", "Account", ACCOUNT_LABELS, account)
               + select("status", "Status", STATUS_LABELS, status)
               + f'<div class="field grow"><label for="rq">Topic contains</label><input id="rq" type="text" name="q" '
               f'value="{esc(q)}" placeholder="e.g. design" maxlength="100" autocomplete="off"></div>'
               '<button>Show</button></form>')

    def cell(label, value, cls=""):
        return f'<td data-label="{label}"{cls}>{value}</td>'
    rows = "".join(
        "<tr>" + cell("Date", esc(m["start"][:16].replace("T", " ")), ' class="nowrap"')
        + cell("Account", esc(ACCOUNT_NAMES.get(m["account"], m["account"])))
        + cell("Team", esc(m["team"].replace("-", " ").capitalize()))
        + f'<td data-label="Topic" dir="auto">{esc(m["topic"]) or "<span class=muted>(no topic)</span>"}</td>'
        + cell("Files", f'{m["verified"]}/{m["files"]}', ' class="num nowrap"')
        + cell("Size", gb(m["bytes"]), ' class="num nowrap"')
        + cell("Status", f'<span class="tag {STATE_TAGS[m["state"]][0]}">{STATE_TAGS[m["state"]][1]}</span>') + "</tr>"
        for m in meetings) or '<tr><td colspan="7" class="empty">No recordings match.</td></tr>'
    nav = ""
    if pages > 1:
        nav = ('<nav class="pager" aria-label="Pages">'
               + (link(account, status, page - 1, "Previous") if page > 1 else "")
               + f"<span>Page {page} of {pages}</span>"
               + (link(account, status, page + 1, "Next") if page < pages else "") + "</nav>")
    t = totals
    count = f"Showing {num(first)}-{num(first + len(meetings) - 1)} of {num(shown)}" if meetings else "Showing 0"
    body = (f'<header>{_brand("All recordings")}<a href="/admin">Back to admin</a></header><main>'
            f'<div class="card quiet"><strong>{num(t["meetings"])} meetings</strong>, {gb(t["bytes"])} &middot; '
            f'{num(t["archived"])} archived, {num(t["pending"])} pending, {num(t["failed"])} failed, '
            f'{num(t.get("skipped", 0))} not copied (short transcript)'
            f'<div class="hint">{count}, newest first, times in UTC</div></div>{filters}{nav}'
            '<div class="scroll"><table class="recs"><caption class="sr-only">Recordings</caption><tr>'
            '<th scope="col">Date (UTC)</th><th scope="col">Account</th><th scope="col">Team</th>'
            '<th scope="col">Topic</th><th scope="col" class="num">Files</th><th scope="col" class="num">Size</th>'
            f'<th scope="col">Status</th></tr>{rows}</table></div>{nav}</main>')
    return _document(nonce, body, script=True, title="All recordings - FloLabs Meetings Archive")
