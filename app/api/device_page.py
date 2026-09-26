"""SAGE's custom Google sign-in bridge. No hosted signup forms or pairing-code UI."""
import html
import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.integrations.clerk_api import _frontend_api_from_publishable_key
from app.services import device_auth

router = APIRouter(include_in_schema=False)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to SAGE AI</title>
<style>
:root{color-scheme:dark light}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#101018;color:#f7f7fb;font:15px/1.6 system-ui,-apple-system,sans-serif;padding:24px}
main{max-width:420px;width:100%;padding:36px;background:#1b1b27;border:1px solid #303040;border-radius:24px}
.brand{font-size:12px;letter-spacing:.18em;color:#b8a6ff;font-weight:700}h1{font-size:26px;line-height:1.25;margin:20px 0 12px}
p{color:#bcbccc}button{width:100%;padding:14px;border:0;border-radius:12px;background:#fff;color:#17171f;font:600 15px system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}.bad{color:#ffadad}.ok{color:#86e7b0}
</style></head><body><main>
<div class="brand">SAGE AI</div><h1 id="title">Your next chapter starts here.</h1>
<p id="msg" role="status">Continue with your Google account. We’ll take you back to SAGE automatically.</p>
<button id="google" hidden>Continue with Google</button><div id="clerk-captcha"></div>
</main><script>
const TICKET = __TICKET__;
const stage = __STAGE__;
const base = location.origin + '/auth/google?ticket=' + encodeURIComponent(TICKET);
const button = document.getElementById('google');
const message = (text, type='') => { document.getElementById('msg').textContent=text; document.getElementById('msg').className=type; };
async function begin() {
  button.disabled = true;
  message('Opening Google sign-in…');
  try {
    await window.Clerk.client.signIn.authenticateWithRedirect({strategy:'oauth_google',
      redirectUrl:base+'&stage=callback', redirectUrlComplete:base+'&stage=complete'});
  } catch(e) { button.disabled=false; button.hidden=false; message('Could not open Google sign-in. Please try again.','bad'); }
}
async function complete() {
  if (!window.Clerk.session) {
    message('Google sign-in was not completed. Please try again.','bad'); button.hidden=false; return;
  }
  const token = await window.Clerk.session.getToken();
  const response = await fetch('/v1/auth/google/complete', {method:'POST',
    headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({ticket:TICKET})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || 'Could not finish sign-in. Please start again from SAGE.');
  document.getElementById('title').textContent='You’re signed in.';
  message('Return to SAGE. Your onboarding or home dashboard will open automatically.','ok');
  button.hidden=true;
  history.replaceState(null,'','/auth/google?stage=done');
}
button.onclick=begin;
window.addEventListener('load', async () => {
  try {
    await window.Clerk.load({signInUrl:base,signUpUrl:base});
    if (stage==='callback') {
      message('Finishing Google sign-in…');
      await window.Clerk.handleRedirectCallback({signInUrl:base,signUpUrl:base,
        signInForceRedirectUrl:base+'&stage=complete',signUpForceRedirectUrl:base+'&stage=complete',
        signUpContinueUrl:base+'&stage=incomplete',secondFactorUrl:base+'&stage=verification'});
    } else if (stage==='complete') { await complete(); }
    else if (stage==='incomplete' || stage==='verification') {
      message('Your account needs additional verification. Please contact SAGE support before trying again.','bad');
    } else if (window.Clerk.session) { await complete(); }
    else { await begin(); }
  } catch(e) { message(e.message || 'Could not load sign-in. Check your connection and try again.','bad'); }
});
</script><script async crossorigin="anonymous" data-clerk-publishable-key="__PK__"
src="https://__FAPI__/npm/@clerk/clerk-js@5/dist/clerk.browser.js"></script></body></html>"""

HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


@router.get("/auth/device", response_class=HTMLResponse)
async def device_page(code: str = ""):
    return HTMLResponse('<h1>Open SAGE to sign in with Google</h1><p>This sign-in link is from an older app version. Please use the updated SAGE app.</p>', headers=HEADERS)


@router.get("/auth/google", response_class=HTMLResponse)
async def google_page(ticket: str = "", stage: str = ""):
    if stage == "done":
        return HTMLResponse('<h1>You’re signed in.</h1><p>Return to SAGE to continue.</p>', headers=HEADERS)
    code = device_auth.verify_browser_ticket(ticket)
    request = await device_auth.describe(code)
    if request["status"] != "pending":
        return HTMLResponse('<h1>Open SAGE to continue</h1><p>This sign-in link has expired or was already used. Start again from the app if needed.</p>', status_code=410, headers=HEADERS)
    settings = get_settings()
    fapi = _frontend_api_from_publishable_key(settings.clerk_publishable_key)
    body = (PAGE.replace('__TICKET__', json.dumps(ticket).replace('<', '\\u003c'))
            .replace('__STAGE__', json.dumps(stage if stage in ('callback','complete','incomplete','verification') else ''))
            .replace('__PK__', html.escape(settings.clerk_publishable_key)).replace('__FAPI__', html.escape(fapi)))
    return HTMLResponse(body, headers=HEADERS)
