import {db,checked,kv,setKv,state,loadConfig,rpc,now} from '../_shared/backend.ts';
import {ready,masked,mergeConfig,inWindow,phone} from '../_shared/domain.mjs';
import {sendEmail,sendWhatsApp} from '../_shared/senders.ts';

const allowedOrigins = (Deno.env.get('SALESQUEST_ALLOWED_ORIGINS') || 'https://popeyaiartificia-png.github.io').split(',');
const ids = (values: any) => {
  if (!Array.isArray(values) || values.length>10000 || values.some(n=>!Number.isSafeInteger(Number(n)) || Number(n)<=0)) throw new Error('Invalid IDs');
  return [...new Set(values.map(Number))];
};
Deno.serve(async req => {
  const origin = req.headers.get('origin');
  const headers: Record<string,string> = {'Content-Type':'application/json','Cache-Control':'no-store','Vary':'Origin',
    'Access-Control-Allow-Methods':'GET,POST,PUT,OPTIONS','Access-Control-Allow-Headers':'authorization,apikey,content-type,x-salesquest'};
  if (origin && allowedOrigins.includes(origin)) headers['Access-Control-Allow-Origin']=origin;
  const reply = (status: number, body: any) => new Response(JSON.stringify(body),{status,headers});
  if (origin && !allowedOrigins.includes(origin)) return reply(403,{error:'This website is not allowed to use the ERP API.'});
  if (req.method==='OPTIONS') return new Response(null,{status:204,headers});
  try {
    const token = req.headers.get('authorization')?.match(/^Bearer\s+(.+)$/i)?.[1];
    if (!token) return reply(401,{error:'Sign in to SalesQuest.'});
    const {data:auth,error:authError} = await db.auth.getUser(token);
    if (authError || !auth.user) return reply(401,{error:'Your session expired. Please sign in again.'});
    const member = checked(await db.from('salesquest_members').select('role').eq('user_id',auth.user.id).maybeSingle());
    if (!member) return reply(403,{error:'This account does not have access to SalesQuest.'});
    const path = new URL(req.url).pathname.split('/salesquest-api')[1] || '/';
    const route = req.method+' '+path;
    if (!['GET','POST','PUT'].includes(req.method)) return reply(405,{error:'Method not allowed'});
    let body: any = {};
    if (req.method!=='GET') {
      if (Number(req.headers.get('content-length') || 0)>30*1024*1024) return reply(413,{error:'Request is too large'});
      const text = await req.text();
      if (text.length>30*1024*1024) return reply(413,{error:'Request is too large'});
      body=JSON.parse(text || '{}');
      if (!body || typeof body!=='object' || Array.isArray(body)) return reply(400,{error:'Invalid request body'});
    }
    if (route==='GET /health') return reply(200,{ok:true,version:1,cloud:true,...ready(await loadConfig())});
    if (route==='GET /state') return reply(200,{rev:Number(await kv('state_rev','0')),state:await state()});
    if (route==='PUT /state') {
      if (!body.state || !Array.isArray(body.state.leads) || !Number.isSafeInteger(body.rev)) return reply(400,{error:'state must be an object with a leads list and revision'});
      const result=await rpc('save_state',{p_rev:body.rev,p_state:body.state});
      return reply(result.error==='conflict'?409:200,result);
    }
    if (route==='GET /config' || route==='PUT /config' || route==='POST /test') {
      if (member.role!=='admin') return reply(403,{error:'Only an ERP administrator can manage messaging settings.'});
      const cfg=await loadConfig();
      if (route==='GET /config') return reply(200,masked(cfg));
      if (route==='PUT /config') {
        const next=mergeConfig(cfg,body);await setKv('config',next);return reply(200,masked(next));
      }
      const to=String(body.to || '').trim();
      if (!to) return reply(400,{error:'Enter where to send the test'});
      if (body.channel==='email') return reply(200,{ok:true,id:await sendEmail(cfg,to,'SalesQuest test email','This is a test from your SalesQuest automation backend.\n\nEmail sending works.')});
      const number=phone(to,(await state())?.settings?.cc || '91');
      if (!number) return reply(400,{error:'That phone number does not look valid'});
      return reply(200,{ok:true,id:await sendWhatsApp(cfg,number,{metaName:body.metaName || 'hello_world',metaLang:body.metaLang || 'en_US'},'',[])});
    }
    if (route==='POST /queue') {
      if (!Array.isArray(body.items) || body.items.length>10000) return reply(400,{error:'Queue up to 10,000 messages at a time'});
      const ts=now(), source=String(body.source || 'manual').slice(0,60);
      const rows=body.items.filter((item:any)=>['email','whatsapp'].includes(item.channel) && typeof item.leadId==='string' && typeof item.tplId==='string')
        .map((item:any)=>({created:ts,due:ts,lead_id:item.leadId,channel:item.channel,tpl_id:item.tplId,source,status:'queued',updated:ts}));
      if (rows.length) checked(await db.from('salesquest_outbox').insert(rows));
      return reply(200,{queued:rows.length});
    }
    if (route==='POST /enroll') {
      if (typeof body.seqId!=='string' || !Array.isArray(body.leadIds) || body.leadIds.length>10000 || body.leadIds.some((id:any)=>typeof id!=='string')) return reply(400,{error:'Invalid sequence enrollment'});
      return reply(200,await rpc('enroll_leads',{p_seq_id:body.seqId,p_lead_ids:body.leadIds}));
    }
    if (route==='POST /enroll/stop') {
      let selected;
      if (body.ids) selected=ids(body.ids);
      else if (typeof body.seqId==='string') selected=checked(await db.from('salesquest_enroll').select('id').eq('seq_id',body.seqId).eq('status','active')).map((row:any)=>row.id);
      else return reply(400,{error:'ids or seqId required'});
      return reply(200,{stopped:await rpc('stop_enrollments',{p_ids:selected})});
    }
    if (route==='POST /outbox/cancel') {
      let query=db.from('salesquest_outbox').update({status:'cancelled',error:'Cancelled by you',updated:now()},{count:'exact'}).eq('status','queued');
      if (!body.all) query=query.in('id',ids(body.ids || []));
      const result=await query;checked(result);return reply(200,{cancelled:result.count || 0});
    }
    if (route==='POST /outbox/retry') {
      checked(await db.from('salesquest_outbox').update({status:'queued',error:'',due:now(),updated:now()}).eq('status','failed').in('id',ids(body.ids || [])));
      return reply(200,{ok:true});
    }
    if (route==='GET /lan') return reply(200,{on:true,cloud:true,urls:[],plain:[],key:''});
    if (route==='GET /poll') {
      const since=Number(new URL(req.url).searchParams.get('since') || 0);
      if (!Number.isSafeInteger(since) || since<0) return reply(400,{error:'Invalid event cursor'});
      const cfg=await loadConfig(), snapshot=await rpc('poll',{p_since:since});
      return reply(200,{...snapshot,...ready(cfg),window:inWindow(cfg),limits:cfg.limits,
        reminder:{enabled:cfg.reminder.enabled,last:await kv('reminder_last',''),error:await kv('reminder_error','')}});
    }
    return reply(404,{error:'Unknown endpoint'});
  } catch (error: any) {
    console.error('API request failed',error.name);
    return reply(error.message?.startsWith('The database operation')?503:400,{error:error.message || 'The request failed. Please retry.'});
  }
});
