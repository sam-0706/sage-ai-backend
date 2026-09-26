"""One-order checkout capability, carried in a fragment and POST body, never an auth token."""
import hashlib
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel,Field
from app.repositories.db import transaction,row
from app.core.config import get_settings
from app.core.errors import NotFound,Forbidden
from app.integrations import razorpay
from app.services import billing
router=APIRouter(tags=['checkout'])
class Ticket(BaseModel):
 token:str=Field(min_length=32,max_length=100)
class Verification(Ticket):
 razorpay_order_id:str
 razorpay_payment_id:str
 razorpay_signature:str
async def order(c,token):
 r=row(await c.fetchrow(f'select {billing.ORDER_COLS} from payment_orders where checkout_token_hash=$1 and checkout_expires_at>now() for update',hashlib.sha256(token.encode()).hexdigest()))
 if not r: raise NotFound('Checkout expired. Open a new checkout from SAGE.')
 return r
@router.post('/checkout/options')
async def options(body:Ticket):
 async with transaction() as c:r=await order(c,body.token)
 return {'key':get_settings().razorpay_key_id,'order_id':r['razorpay_order_id'],'amount':r['amount'],'currency':r['currency'],'name':'SAGE AI','description':'TEST payment · '+('Demo campus fee, not paid to BITSoM' if r['purpose']=='fee' else r['plan_code']),'status':r['status']}
@router.post('/checkout/verify')
async def verify(body:Verification):
 async with transaction() as c:
  r=await order(c,body.token)
  if r['razorpay_order_id']!=body.razorpay_order_id or not razorpay.verify_payment_signature(body.razorpay_order_id,body.razorpay_payment_id,body.razorpay_signature):raise Forbidden('Payment signature verification failed')
  r=await billing._settle(c,r,body.razorpay_payment_id,'hosted_checkout',r['user_id'])
 return {'status':r['status']}
@router.get('/checkout',response_class=HTMLResponse)
async def page():
 return HTMLResponse('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SAGE test checkout</title><style>body{font:16px system-ui;background:#f4f3fb;color:#1c1830;display:grid;place-items:center;min-height:90vh}main{background:white;border-radius:24px;padding:36px;max-width:460px}button{padding:14px 24px;border:0;border-radius:12px;background:#8050d9;color:white;font-weight:700}p{line-height:1.6}</style><main><h1>SAGE AI · Test checkout</h1><p>No real money is collected. Complete the Razorpay test flow yourself, then return to SAGE and press Refresh access.</p><button id="pay" disabled>Loading order…</button><p id="msg" role="status"></p></main><script src="https://checkout.razorpay.com/v1/checkout.js"></script><script>
const token=location.hash.slice(1);history.replaceState(null,'','/checkout');const msg=document.querySelector('#msg'),pay=document.querySelector('#pay');
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error?.message||'Request failed');return d}
(async()=>{try{const o=await post('/checkout/options',{token});if(o.status==='paid'){pay.textContent='Already paid';return}pay.textContent='Pay ₹'+(o.amount/100).toLocaleString('en-IN')+' · TEST';pay.disabled=false;pay.onclick=()=>{if(!window.Razorpay){msg.textContent='Razorpay could not load. Check your connection.';return}new Razorpay({...o,handler:async data=>{pay.disabled=true;try{await post('/checkout/verify',{token,...data});msg.textContent='Test payment verified. Return to SAGE and refresh access.';pay.textContent='Payment verified'}catch(e){msg.textContent=e.message;pay.disabled=false}},modal:{ondismiss:()=>msg.textContent='Checkout closed. Your access is unchanged.'}}).open()}}catch(e){msg.textContent=e.message;pay.textContent='Unavailable'}})();</script></html>''',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
