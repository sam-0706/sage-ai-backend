"""Hosted browser page for device sign-in. Uses Clerk's browser SDK with the publishable key only."""
import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.integrations.clerk_api import _frontend_api_from_publishable_key

router = APIRouter(include_in_schema=False)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to SAGE AI</title>
<style>
 :root{--bg:#0f0e17;--card:#1a1826;--ink:#f3f2f8;--mut:#a7a3bd;--acc:#8b5cf6;--ok:#22c55e;--bad:#ef4444}
 @media (prefers-color-scheme: light){:root{--bg:#f6f5fb;--card:#fff;--ink:#16141f;--mut:#5d5972}}
 *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
 font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
 .card{width:100%;max-width:440px;background:var(--card);border-radius:18px;padding:28px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
 h1{font-size:22px;margin:0 0 6px}p{color:var(--mut);margin:0 0 16px}
 .code{font:700 28px/1 ui-monospace,Menlo,monospace;letter-spacing:.12em;text-align:center;padding:16px;border-radius:12px;
 background:rgba(139,92,246,.12);color:var(--acc);margin:12px 0 18px}
 button{width:100%;height:46px;border:0;border-radius:12px;font-weight:700;font-size:15px;cursor:pointer}
 .go{background:var(--acc);color:#fff}.no{background:transparent;color:var(--mut);margin-top:8px}
 .ok{color:var(--ok)}.bad{color:var(--bad)}#signin{display:flex;justify-content:center}
</style></head>
<body><main class="card">
 <h1>Sign in to SAGE AI</h1>
 <p id="lead">Confirm this code matches the one shown in your SAGE app.</p>
 <div class="code">__CODE__</div>
 <div id="signin"></div>
 <div id="approve" hidden>
   <p id="who"></p>
   <button class="go" id="yes">Approve sign-in on __CLIENT__</button>
   <button class="no" id="nope">This wasn't me</button>
 </div>
 <p id="msg" role="status"></p>
</main>
<script>
 const CODE = "__CODE__";
 const msg = (t, cls) => { const m = document.getElementById('msg'); m.textContent = t; m.className = cls || '' };
 async function decide(approve) {
   try {
     const token = await window.Clerk.session.getToken();
     const r = await fetch('/v1/auth/device/' + (approve ? 'approve' : 'deny'), {method: 'POST',
       headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token}, body: JSON.stringify({user_code: CODE})});
     const d = await r.json();
     document.getElementById('approve').hidden = true;
     if (!r.ok) {
       const c = d.error && d.error.code;
       msg(c === 'not_on_waitlist' ? "This email isn't on the SAGE AI beta list yet. Sign in with the email you joined the waitlist with."
                                  : (d.error && d.error.message) || 'Something went wrong', 'bad');
       if (c === 'not_on_waitlist') setTimeout(() => window.Clerk.signOut().then(() => location.reload()), 4000);
       return;
     }
     msg(approve ? 'Approved. You can return to the SAGE app — it will finish signing in automatically.' : 'Sign-in request denied.', approve ? 'ok' : '');
   } catch (e) { msg('Network error — please retry.', 'bad') }
 }
 async function render() {
   const s = await (await fetch('/v1/auth/device/' + encodeURIComponent(CODE))).json().catch(() => null);
   if (!s || s.status !== 'pending') { msg(s && s.status === 'expired' ? 'This code has expired. Start sign-in again from the app.' :
                                            'This code is no longer valid. Start sign-in again from the app.', 'bad'); return; }
   if (window.Clerk.user) {
     document.getElementById('signin').innerHTML = '';
     document.getElementById('who').textContent = 'Signed in as ' + window.Clerk.user.primaryEmailAddress.emailAddress + '.';
     document.getElementById('approve').hidden = false;
   } else {
     window.Clerk.mountSignIn(document.getElementById('signin'), {forceRedirectUrl: location.href, signUpForceRedirectUrl: location.href});
   }
 }
 document.getElementById('yes').onclick = () => decide(true);
 document.getElementById('nope').onclick = () => decide(false);
 window.addEventListener('load', async () => {
   try { await window.Clerk.load(); window.Clerk.addListener(() => render()); render(); }
   catch (e) { msg('Could not load sign-in. Check your connection and reload.', 'bad') }
 });
</script>
<script async crossorigin="anonymous" data-clerk-publishable-key="__PK__"
 src="https://__FAPI__/npm/@clerk/clerk-js@5/dist/clerk.browser.js"></script>
</body></html>"""


@router.get("/auth/device", response_class=HTMLResponse)
async def device_page(code: str = ""):
    s = get_settings()
    safe_code = "".join(c for c in code.upper() if c.isalnum() or c == "-")[:9]
    fapi = _frontend_api_from_publishable_key(s.clerk_publishable_key)
    body = (PAGE.replace("__CODE__", html.escape(safe_code)).replace("__CLIENT__", "your desktop")
            .replace("__PK__", html.escape(s.clerk_publishable_key)).replace("__FAPI__", html.escape(fapi)))
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
