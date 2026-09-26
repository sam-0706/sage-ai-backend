"""Bounded real OpenAI smoke test with an isolated test student; no outbound calls/payments."""
import asyncio
from uuid import uuid4
from app.repositories.db import transaction,close_pool
from app.services import campus,billing,profile
from app.domain.campus import PlanRequest
async def main():
 uid=uuid4()
 try:
  async with transaction() as c:
   await c.execute("insert into users(id,email,full_name,mode,status) values($1,$2,'Campus Smoke Test','student','active')",uid,str(uid)+'@example.invalid')
   await billing.grant_plan(c,uid,'pro',source='test',actor_id=None)
   await profile.upsert_profile(c,uid,'student',{'subjects':['Marketing Management','Business Statistics I'],'daily_minutes':60,'salary_lpa':24,'target_role':'Product Analyst','career_goal':'Prepare a product analytics portfolio','specialisation':'Ecommerce and Digital Leadership','skills':['SQL','analytics']},None,{'voice_calls':False,'deadline_calls':False})
  r=await campus.recommendations(uid);assert len(r['items'])==8;assert all(0<=j['score']<=100 for j in r['items']);print('PASS real embedding recommendations:',len(r['items']))
  p=await campus.generate(uid,PlanRequest(goal='Build a product analytics portfolio',weeks=2));assert p['output']['tasks'];print('PASS real structured plan:',len(p['output']['tasks']),'tasks')
  w=await campus.workspace(uid);assert len(w['tasks'])==len(p['output']['tasks']);print('PASS persisted private workspace')
 finally:
  async with transaction() as c:await c.execute('delete from users where id=$1',uid)
  await close_pool()
if __name__=='__main__':asyncio.run(main())
