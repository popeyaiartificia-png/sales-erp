-- Preserve the local backend default for an empty stop list. Keep the sending gap
-- across user retries, which change an outbox row back to queued.

create or replace function public.salesquest_enroll_leads(p_seq_id text, p_lead_ids text[])
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare st jsonb; seq jsonb; steps jsonb; lead jsonb; lid text; inserted integer;
    added integer := 0; skipped integer := 0; ts double precision := extract(epoch from now());
begin
    select v::jsonb into st from public.salesquest_kv where k='state';
    select value into seq from jsonb_array_elements(coalesce(st->'sequences','[]')) where value->>'id'=p_seq_id;
    select jsonb_agg(value) into steps from jsonb_array_elements(coalesce(seq->'steps','[]')) where coalesce(value->>'tplId','') <> '';
    if seq is null or steps is null then
        raise exception 'Sequence not found or has no steps. Save it first.' using errcode='22023';
    end if;
    foreach lid in array p_lead_ids loop
        select value into lead from jsonb_array_elements(st->'leads') where value->>'id'=lid;
        if lead is null or coalesce(nullif(nullif(seq->'stopOn','null'::jsonb),'[]'::jsonb),'["won","lost","invalid"]'::jsonb) ? (lead->>'status') then
            skipped := skipped+1;
            continue;
        end if;
        insert into public.salesquest_enroll (seq_id,lead_id,started,step,next_at,status,updated)
            values(p_seq_id,lid,ts,0,ts+coalesce((steps->0->>'day')::double precision,0)*86400,'active',ts)
            on conflict do nothing;
        get diagnostics inserted = row_count;
        added := added+inserted;
        skipped := skipped+1-inserted;
    end loop;
    return jsonb_build_object('enrolled',added,'skipped',skipped);
end;
$$;

create or replace function public.salesquest_run_sequences()
returns integer language plpgsql security invoker set search_path = '' as $$
declare st jsonb; seq jsonb; lead jsonb; steps jsonb; tpl jsonb; e record; why text;
    n integer := 0; ts double precision := extract(epoch from now());
begin
    select v::jsonb into st from public.salesquest_kv where k='state';
    if st is null then return 0; end if;
    for e in select * from public.salesquest_enroll where status='active' and next_at<=ts
        order by next_at,id limit 1000 for update skip locked loop
        seq := null; lead := null; tpl := null; why := null;
        select value into seq from jsonb_array_elements(coalesce(st->'sequences','[]')) where value->>'id'=e.seq_id;
        select value into lead from jsonb_array_elements(st->'leads') where value->>'id'=e.lead_id;
        if seq is null then why := 'Sequence was deleted';
        elsif lead is null then why := 'Lead was deleted';
        elsif coalesce(nullif(nullif(seq->'stopOn','null'::jsonb),'[]'::jsonb),'["won","lost","invalid"]'::jsonb) ? (lead->>'status') then why := 'Lead is closed';
        end if;
        if why is not null then
            update public.salesquest_enroll set status='stopped', reason=why, updated=ts where id=e.id;
            update public.salesquest_outbox set status='cancelled', error=why, updated=ts where enroll_id=e.id and status='queued';
            continue;
        end if;
        if seq->'active' = 'false'::jsonb then continue; end if;
        select coalesce(jsonb_agg(value),'[]') into steps from jsonb_array_elements(coalesce(seq->'steps','[]')) where coalesce(value->>'tplId','') <> '';
        if e.step>=jsonb_array_length(steps) then
            update public.salesquest_enroll set status='done', updated=ts where id=e.id;
            continue;
        end if;
        select value into tpl from jsonb_array_elements(coalesce(st->'templates','[]')) where value->>'id'=steps->e.step->>'tplId';
        if tpl is null then
            update public.salesquest_enroll set status='stopped', reason='A template in this sequence was deleted', updated=ts where id=e.id;
            update public.salesquest_outbox set status='cancelled', error='A template in this sequence was deleted', updated=ts where enroll_id=e.id and status='queued';
            continue;
        end if;
        insert into public.salesquest_outbox(created,due,lead_id,channel,tpl_id,source,enroll_id,status,updated)
            values(ts,ts,e.lead_id,tpl->>'channel',tpl->>'id','seq:'||e.seq_id,e.id,'queued',ts);
        if e.step+1<jsonb_array_length(steps) then
            update public.salesquest_enroll set step=e.step+1,
                next_at=e.started+coalesce((steps->(e.step+1)->>'day')::double precision,0)*86400, updated=ts where id=e.id;
        else
            update public.salesquest_enroll set step=e.step+1,status='done',updated=ts where id=e.id;
        end if;
        n := n+1;
    end loop;
    return n;
end;
$$;

create or replace function public.salesquest_claim_message(p_channel text, p_config jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare item public.salesquest_outbox; ts double precision := extract(epoch from now());
    local_now timestamp := now() at time zone 'Asia/Kolkata'; limits jsonb := p_config->'limits';
    cap integer; attempts bigint; last_attempt double precision;
begin
    if p_channel not in ('email','whatsapp') then raise exception 'Invalid channel'; end if;
    perform pg_advisory_xact_lock(hashtext('salesquest:'||p_channel));
    -- An interrupted delivery may have reached the provider. Never retry it automatically.
    update public.salesquest_outbox set status='failed', error='Delivery status unknown after worker interruption. Check the provider before retrying.',updated=ts
        where channel=p_channel and status='sending' and updated<ts-120;
    if not (limits->'days' @> jsonb_build_array(extract(isodow from local_now)::integer-1))
        or local_now::time < (limits->>'windowStart')::time or local_now::time >= (limits->>'windowEnd')::time then
        return null;
    end if;
    cap := (limits->>(case when p_channel='email' then 'emailPerDay' else 'waPerDay' end))::integer;
    select count(*) into attempts from public.salesquest_outbox where channel=p_channel and
        ((status='sent' and sent_at>=extract(epoch from (local_now::date::timestamp at time zone 'Asia/Kolkata'))) or status='sending');
    if attempts>=cap then return null; end if;
    select v::double precision into last_attempt from public.salesquest_kv
        where k='last_attempt_'||p_channel;
    if last_attempt is not null and ts-last_attempt<greatest(3,(limits->>'gapSec')::integer) then return null; end if;
    select * into item from public.salesquest_outbox where channel=p_channel and status='queued' and due<=ts
        order by due,id limit 1 for update skip locked;
    if item.id is null then return null; end if;
    insert into public.salesquest_kv(k,v) values('last_attempt_'||p_channel,ts::text)
        on conflict(k) do update set v=excluded.v;
    update public.salesquest_outbox set status='sending',updated=ts where id=item.id;
    return to_jsonb(item);
end;
$$;
