"""Read-only schema/workspace smoke check. Does not dispatch calls or settle payments."""
import asyncio
from app.repositories.db import transaction,close_pool
from app.services import campus,billing
async def main():
 try:
  async with transaction() as c:
   uid=await c.fetchval('select id from users limit 1')
   plans=await billing.list_plans(c)
   print('Pricing:',[(p['code'],p['price_inr_paise'],p['voice_minutes']) for p in plans if p['code'] in ('plus','pro','ultra')])
   print('RLS:',[(r['relname'],r['relrowsecurity']) for r in await c.fetch("select relname,relrowsecurity from pg_class where relname in ('campus_plans','campus_tasks','campus_deadlines','campus_opportunities')")])
  if uid:
   w=await campus.workspace(uid);print('Workspace:',len(w['opportunities']),'opportunities;',len(w['catalogue']['courses']),'course entries')
 finally:await close_pool()
if __name__=='__main__':asyncio.run(main())
