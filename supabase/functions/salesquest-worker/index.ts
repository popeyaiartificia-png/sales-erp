import {db,checked,kv,setKv,state,loadConfig,rpc,now} from '../_shared/backend.ts';
import {ready,indiaTime,minute,CLOSED,firstOf,phone,fill,optedOut,stopReason} from '../_shared/domain.mjs';
import {sendEmail,sendWhatsApp} from '../_shared/senders.ts';

async function authenticated(req: Request) {
  const expected=Deno.env.get('SALESQUEST_WORKER_TOKEN') || '';
  const supplied=req.headers.get('x-worker-token') || '';
  if (!expected || !supplied) return false;
  const digest=async(value:string)=>new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)));
  const [a,b]=await Promise.all([digest(expected),digest(supplied)]);
  let difference=0;for(let i=0;i<a.length;i++) difference |= a[i]^b[i];
  return difference===0;
}
async function finish(id: number, status: string, extra: any = {}) {
  checked(await db.from('salesquest_outbox').update({...extra,status,updated:now()}).eq('id',id).eq('status','sending'));
}
async function deliver(row: any, cfg: any) {
  const st=await state() || {}, settings=st.settings || {};
  const lead=(st.leads || []).find((l:any)=>l.id===row.lead_id);
  const tpl=(st.templates || []).find((t:any)=>t.id===row.tpl_id);
  if (!lead) return await finish(row.id,'skipped',{error:'Lead was deleted'});
  if (!tpl) return await finish(row.id,'skipped',{error:'Template was deleted'});
  if (row.enroll_id) {
    const enrollment=checked(await db.from('salesquest_enroll').select('*').eq('id',row.enroll_id).maybeSingle());
    const reason=!enrollment || enrollment.status==='stopped' ? 'Sequence was stopped for this lead' : stopReason((st.sequences || []).find((s:any)=>s.id===enrollment.seq_id),lead);
    if (reason) return await finish(row.id,'cancelled',{error:reason});
  }
  const subject=fill(tpl.subject,lead,settings), body=fill(tpl.body,lead,settings);
  let to='';
  try {
    if (/\[your (name|company)\]/.test(body+subject)) throw new Error('Add your name and company in Settings first.');
    let provider;
    if (row.channel==='email') {
      to=firstOf(lead.email);
      if (!to) return await finish(row.id,'skipped',{error:'Lead has no email address'});
      if (optedOut(cfg,to,'')) return await finish(row.id,'skipped',{to_addr:to,error:'On the opt-out list'});
      provider=await sendEmail(cfg,to,subject,body);
    } else {
      to=phone(lead.phone,settings.cc || '91');
      if (!to) return await finish(row.id,'skipped',{error:'Lead has no valid phone number'});
      if (optedOut(cfg,'',to)) return await finish(row.id,'skipped',{to_addr:to,error:'On the opt-out list'});
      const params=String(tpl.metaParams || '').split(',').map(p=>p.trim()).filter(Boolean).map(p=>fill('{{'+p+'}}',lead,settings));
      provider=await sendWhatsApp(cfg,to,tpl,body,params);
    }
    await finish(row.id,'sent',{to_addr:to,subject,body,sent_at:now(),provider_id:provider,error:''});
  } catch(error: any) {
    await finish(row.id,'failed',{to_addr:to,subject,body,error:String(error.message || 'Delivery failed').slice(0,1000)});
  }
}
async function reminder(cfg: any) {
  const time=indiaTime(), r=cfg.reminder;
  if (!r.enabled || !r.to || !ready(cfg).email || time.minute<minute(r.time)) return false;
  if (!await rpc('claim_reminder')) return false;
  const st=await state();if(!st) return false;
  const leads=(st.leads || []).filter((l:any)=>!CLOSED.includes(l.status));
  const due=leads.filter((l:any)=>l.nextFollowUp && l.nextFollowUp<=time.date).sort((a:any,b:any)=>a.nextFollowUp.localeCompare(b.nextFollowUp));
  const hot=leads.filter((l:any)=>l.temp==='hot' && !due.includes(l));
  const yesterday=indiaTime(new Date(Date.now()-86400000)).date;
  const calls=(st.log || []).filter((entry:any)=>entry.type==='call' && indiaTime(new Date(entry.ts)).date===yesterday).length;
  const line=(l:any)=>'- '+l.name+' | '+(l.phone || l.email || '-')+' | '+(l.nextFollowUp?'due '+l.nextFollowUp:l.temp || '');
  const body=['Good morning '+(st.settings?.myName || 'there')+',','',`Yesterday: ${calls} calls logged. Today's target: ${st.settings?.dailyTarget || 100} calls.`,'',`FOLLOW-UPS DUE (${due.length})`,...due.slice(0,60).map(line),'',`HOT LEADS (${hot.length})`,...hot.slice(0,30).map(line),'','Open SalesQuest: '+(Deno.env.get('SALESQUEST_APP_URL') || '')].join('\n');
  try {
    await sendEmail(cfg,r.to,`SalesQuest: ${due.length} follow-ups due, ${hot.length} hot leads (${time.date})`,body);
    await setKv('reminder_last',time.date);await setKv('reminder_error','');return true;
  } catch(error: any) {await setKv('reminder_error',String(error.message || 'Reminder delivery failed').slice(0,1000));return false}
}
Deno.serve(async req => {
  const headers={'Content-Type':'application/json','Cache-Control':'no-store'};
  const reply=(status:number,body:any)=>new Response(JSON.stringify(body),{status,headers});
  if (req.method!=='POST' || !await authenticated(req)) return reply(401,{error:'Worker authentication required'});
  try {
    const scheduled=await rpc('run_sequences'), cfg=await loadConfig(), enabled=ready(cfg);
    let processed=0;
    for (const channel of ['whatsapp','email']) {
      if (!enabled[channel==='whatsapp'?'wa':'email']) continue;
      const row=await rpc('claim_message',{p_channel:channel,p_config:cfg});
      if (row) {await deliver(row,cfg);processed++}
    }
    return reply(200,{ok:true,scheduled,processed,reminder:await reminder(cfg)});
  } catch(error: any) {console.error('Worker failed',error.name);return reply(500,{error:'The worker could not complete this run. Check server logs.'})}
});
