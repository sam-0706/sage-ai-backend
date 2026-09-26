from uuid import UUID
from fastapi import APIRouter, Depends
from app.auth.deps import Principal,get_principal
from app.domain.campus import PlanRequest, TaskUpdate, DeadlineIn, InterviewIn, CheckoutIn, JobDiscoveryIn
from app.services import campus,billing
from app.repositories.db import transaction,row
from app.core.errors import NotFound,AppError
router=APIRouter(prefix='/campus',tags=['campus'])

@router.get('/catalogue')
async def catalogue(p:Principal=Depends(get_principal)): return campus.catalog()
@router.get('/workspace')
async def workspace(p:Principal=Depends(get_principal)): return await campus.workspace(p.id)
@router.post('/plans')
async def generate(body:PlanRequest,p:Principal=Depends(get_principal)): return await campus.generate(p.id,body)
@router.post('/recommendations')
async def recommend(p:Principal=Depends(get_principal)): return await campus.recommendations(p.id)
@router.post('/jobs/discover')
async def discover_jobs(body:JobDiscoveryIn,p:Principal=Depends(get_principal)): return await campus.discover_jobs(p.id,body)
@router.post('/interview')
async def interview(body:InterviewIn,p:Principal=Depends(get_principal)): return await campus.interview(p.id,body.job_id,body.resume_text)
@router.patch('/tasks/{task_id}')
async def task(task_id:UUID,body:TaskUpdate,p:Principal=Depends(get_principal)):
 async with transaction() as c:
  result=row(await c.fetchrow("update campus_tasks set status=$3,evidence=$4,completed_at=case when $3='done' then now() else null end where id=$1 and user_id=$2 returning *",task_id,p.id,body.status,body.evidence))
 if not result: raise NotFound('Task not found')
 return result
@router.post('/deadlines/demo')
async def demo(p:Principal=Depends(get_principal)): return await campus.seed_deadlines(p.id)
@router.post('/deadlines')
async def deadline(body:DeadlineIn,p:Principal=Depends(get_principal)):
 async with transaction() as c:
  return row(await c.fetchrow('insert into campus_deadlines(user_id,title,category,due_at,amount) values($1,$2,$3,$4,$5) returning *',p.id,body.title,body.category,body.due_at,body.amount))
@router.post('/deadlines/{deadline_id}/complete')
async def complete(deadline_id:UUID,p:Principal=Depends(get_principal)):
 async with transaction() as c:
  r=row(await c.fetchrow("update campus_deadlines set status='done' where id=$1 and user_id=$2 and category='assignment' returning *",deadline_id,p.id))
 if not r: raise NotFound('Assignment not found; fees require verified checkout')
 return r
@router.post('/deadlines/{deadline_id}/checkout')
async def checkout(deadline_id:UUID,body:CheckoutIn,p:Principal=Depends(get_principal)):
 return await billing.create_fee_order(p,deadline_id,body.idempotency_key)

@router.post('/reminders/check')
async def reminders(p:Principal=Depends(get_principal)): return await campus.check_reminders(p.id)
