import asyncio
from datetime import UTC,datetime,timedelta
from app.data.campus import JOBS
from app.repositories.db import transaction,close_pool
async def main():
 async with transaction() as c:
  for id,kind,title,org,salary,skills,special,description in JOBS:
   await c.execute('''insert into campus_opportunities(id,kind,title,organisation,location,salary_lpa,skills,specialisations,description,event_at)
   values($1,$2,$3,$4,'Mumbai / Hybrid (demo)',$5,$6,$7,$8,$9) on conflict(id) do update set title=excluded.title,description=excluded.description''',id,kind,title,org,salary,skills,special,description,datetime.now(UTC)+timedelta(days=5) if kind in ('workshop','networking') else None)
 print('Seeded',len(JOBS),'explicitly synthetic opportunities')
 await close_pool()
if __name__=='__main__':asyncio.run(main())
