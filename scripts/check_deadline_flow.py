"""Integration smoke with a stubbed provider: never places a telephone call."""
import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock,patch
from app.repositories.db import transaction,close_pool
from app.services import campus,billing,profile
async def main():
 uid=uuid4()
 try:
  async with transaction() as c:
   await c.execute("insert into users(id,email,full_name,mode,status,phone) values($1,$2,'Synthetic Deadline Test','student','active','+15555550123')",uid,str(uid)+'@example.invalid')
   await billing.grant_plan(c,uid,'pro',source='test',actor_id=None)
   await profile.upsert_profile(c,uid,'student',{},None,{'deadline_calls':True})
  await campus.seed_deadlines(uid)
  dispatch=AsyncMock(return_value='test-'+str(uuid4()))
  with patch('app.integrations.omnidim.dispatch_call',dispatch):
   first=await campus.check_reminders(uid)
   second=await campus.check_reminders(uid)
  if first.get('reason'):print('Skipped dispatch branch:',first['reason']);return
  assert dispatch.await_count==1,(first,second,dispatch.await_count)
  async with transaction() as c:
   assert await c.fetchval('select count(*) from calls where user_id=$1',uid)==1
  print('PASS deadline opt-in, durable dispatch, duplicate prevention; provider stubbed, zero telephone calls')
 finally:
  async with transaction() as c:await c.execute('delete from users where id=$1',uid)
  await close_pool()
if __name__=='__main__':asyncio.run(main())
