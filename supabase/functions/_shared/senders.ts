import nodemailer from 'npm:nodemailer@10.6.0';
import {ready} from './domain.mjs';

export async function sendEmail(cfg: any, to: string, subject: string, text: string) {
  if (!ready(cfg).email) throw new Error('Email is not set up yet. Add your SMTP settings first.');
  const e = cfg.email;
  const transport = nodemailer.createTransport({host:e.host, port:Number(e.port), secure:Number(e.port)===465,
    requireTLS:Number(e.port)!==465, auth:{user:e.user,pass:e.password}, connectionTimeout:15000,
    greetingTimeout:15000, socketTimeout:30000});
  try {
    const result = await transport.sendMail({from:{name:e.fromName || e.user,address:e.user},to,subject:subject || '(no subject)',text});
    return result.messageId;
  } catch (error: any) {
    if (error.code==='EAUTH') throw new Error('Email login was rejected. For Gmail, use an App Password.');
    throw new Error('Email delivery failed ('+String(error.code || 'SMTP')+'). Check your SMTP settings and provider.');
  } finally {transport.close()}
}
export async function sendWhatsApp(cfg: any, to: string, template: any, text: string, params: string[]) {
  if (!ready(cfg).wa) throw new Error('WhatsApp is not set up yet. Add your Meta settings first.');
  const w = cfg.wa;
  let payload: any = {messaging_product:'whatsapp',to,type:'text',text:{preview_url:false,body:text}};
  if (template.metaName) {
    const meta: any = {name:template.metaName.trim(),language:{code:(template.metaLang || 'en').trim()}};
    if (params.length) meta.components=[{type:'body',parameters:params.map(p=>({type:'text',text:p.replace(/\s+/g,' ').trim() || '-'}))}];
    payload={messaging_product:'whatsapp',to,type:'template',template:meta};
  }
  const response = await fetch(`https://graph.facebook.com/${w.version}/${w.phoneId}/messages`,{
    method:'POST',headers:{Authorization:'Bearer '+w.token,'Content-Type':'application/json'},
    body:JSON.stringify(payload),signal:AbortSignal.timeout(30000)});
  const result = await response.json();
  if (!response.ok) {
    const code = String(result.error?.code || response.status);
    throw new Error(code==='131047' ? 'WhatsApp requires a Meta-approved template outside the 24-hour conversation window.' : 'WhatsApp delivery failed (code '+code+'). Check Meta settings and template approval.');
  }
  return result.messages?.[0]?.id || '';
}
