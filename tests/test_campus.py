from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from pydantic import ValidationError
from app.api.v1.onboarding import OnboardingIn
from app.services import billing,campus
from app.domain.campus import DiscoveredJob, JobDiscoveryIn, JobDiscoveryResult
from app.integrations.openai_client import AIResult
from app.core.errors import Forbidden

def test_catalogue_has_terms_and_source():
 c=campus.catalog()
 assert len(c['courses'])==80
 assert {r['term'] for r in c['courses']}=={1,2,3,4,5,6}
 assert len(c['specialisations'])==6
 assert c['source'].startswith('https://')

def test_onboarding_refuses_missing_phone_with_automatic_consent():
 with pytest.raises(ValidationError):OnboardingIn(full_name='Student',mode='student',deadline_call_consent=True)

@pytest.mark.parametrize('minutes',[0,481,'not a number'])
def test_onboarding_checks_time_budget(minutes):
 with pytest.raises(ValidationError):
  OnboardingIn(full_name='Student',mode='student',profile={'onboarding_version':2,'daily_minutes':minutes,'salary_lpa':24,'semester':1,'batch':2026,'subjects':['Statistics']})

@pytest.mark.asyncio
async def test_plus_cannot_use_interview(monkeypatch):
 monkeypatch.setattr(campus.entitlements,'active_subscription',AsyncMock(return_value={'plan_code':'plus','features':['semester_planner']}))
 with pytest.raises(Forbidden):await campus.feature(None,uuid4(),'interview_ai')

@pytest.mark.asyncio
async def test_fee_payment_does_not_grant_subscription(monkeypatch):
 uid,did,oid=uuid4(),uuid4(),uuid4()
 order={'id':oid,'user_id':uid,'deadline_id':did,'purpose':'fee','status':'pending','amount':50000,'razorpay_order_id':'order_test'}
 conn=AsyncMock();conn.fetchrow.return_value={**order,'status':'paid'}
 monkeypatch.setattr(billing.razorpay,'fetch_payment',AsyncMock(return_value={'order_id':'order_test','amount':50000,'status':'captured'}))
 grant=AsyncMock();monkeypatch.setattr(billing,'grant_plan',grant);monkeypatch.setattr(billing.audit,'record',AsyncMock())
 await billing._settle(conn,order,'pay_test','test',uid)
 grant.assert_not_awaited();assert conn.execute.call_args.args[1:]==(did,uid)

@pytest.mark.asyncio
async def test_paid_order_is_idempotent(monkeypatch):
 fetch=AsyncMock();monkeypatch.setattr(billing.razorpay,'fetch_payment',fetch)
 assert await billing._settle(AsyncMock(),{'status':'paid'},'pay_test','test',None)=={'status':'paid'}
 fetch.assert_not_awaited()

@pytest.mark.asyncio
async def test_live_job_discovery_uses_profile_and_limits_results(monkeypatch):
 uid=uuid4(); conn=AsyncMock()
 monkeypatch.setattr(campus,'transaction',lambda: _AsyncContext(conn))
 monkeypatch.setattr(campus,'feature',AsyncMock())
 monkeypatch.setattr(campus.entitlements,'consume',AsyncMock())
 monkeypatch.setattr(campus.profile,'get_profile',AsyncMock(return_value={'data':{'target_role':'Product Manager','skills':['SQL','research'],'salary_lpa':30}}))
 jobs=[DiscoveredJob(title=f'Role {i}',company='Company',location='Mumbai',work_mode='hybrid',employment_type='Full-time',
  source_name='Careers',source_url=f'https://example.com/jobs/{i}',apply_url=f'https://example.com/apply/{i}',skills=['SQL'],match_score=90-i,why_it_fits=['SQL'],gaps=[]) for i in range(5)]
 search=AsyncMock(return_value=AIResult(data=JobDiscoveryResult(summary='Five matches',jobs=jobs,searched_at='2026-09-26T10:00:00+05:30',search_notes=[]),run_id=uuid4(),model='gpt-5-mini'))
 monkeypatch.setattr(campus.openai_client,'web_search_structured',search)
 result=await campus.discover_jobs(uid,JobDiscoveryIn())
 assert len(result['items'])==5
 assert 'Product Manager' in search.call_args.kwargs['input_text']
 campus.entitlements.consume.assert_awaited_once_with(conn,uid,'ai_requests')

class _AsyncContext:
 def __init__(self,value): self.value=value
 async def __aenter__(self): return self.value
 async def __aexit__(self,*_): return False
