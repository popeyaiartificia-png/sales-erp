import {createClient} from 'npm:@supabase/supabase-js@2.119.0';
import {config} from './domain.mjs';

const url = Deno.env.get('SUPABASE_URL')!;
const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || JSON.parse(Deno.env.get('SUPABASE_SECRET_KEYS') || '{}').default;
export const db = createClient(url, key, {auth: {persistSession: false, autoRefreshToken: false}});
export function checked(result: any) {
  if (result.error) {
    console.error('Database operation failed', result.error.code);
    throw new Error('The database operation failed. Please retry.');
  }
  return result.data;
}
export async function kv(name: string, fallback: any = null) {
  const row = checked(await db.from('salesquest_kv').select('v').eq('k',name).maybeSingle());
  return row?.v ?? fallback;
}
export async function setKv(name: string, value: any) {
  checked(await db.from('salesquest_kv').upsert({k: name, v: typeof value === 'string' ? value : JSON.stringify(value)}));
}
export async function state() {return JSON.parse(await kv('state','null'))}
export async function loadConfig() {return config(JSON.parse(await kv('config','{}')))}
export async function rpc(name: string, args = {}) {return checked(await db.rpc('salesquest_'+name,args))}
export const now = () => Date.now()/1000;
