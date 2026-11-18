create index salesquest_outbox_daily on public.salesquest_outbox (channel, status, sent_at);

create function public.salesquest_record_event() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
    if new.status is distinct from old.status and new.status in ('sent','failed','skipped','cancelled') then
        update public.salesquest_kv set v = (v::bigint + 1)::text where k = 'ev'
            returning v::bigint into new.ev;
    end if;
    return new;
end;
$$;
create trigger salesquest_outbox_event before update on public.salesquest_outbox
    for each row execute function public.salesquest_record_event();

create function public.salesquest_stop_enrollments(p_ids bigint[])
returns integer language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    update public.salesquest_enroll set status='stopped', reason='Stopped by you',
        updated=extract(epoch from now()) where id=any(p_ids) and status <> 'stopped';
    get diagnostics n = row_count;
    update public.salesquest_outbox set status='cancelled', error='Stopped by you',
        updated=extract(epoch from now()) where enroll_id=any(p_ids) and status='queued';
    return n;
end;
$$;

create function public.salesquest_enroll_leads(p_seq_id text, p_lead_ids text[])
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
        if lead is null or coalesce(seq->'stopOn','["won","lost","invalid"]'::jsonb) ? (lead->>'status') then
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

create function public.salesquest_run_sequences()
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
        elsif coalesce(seq->'stopOn','["won","lost","invalid"]'::jsonb) ? (lead->>'status') then why := 'Lead is closed';
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

create function public.salesquest_claim_message(p_channel text, p_config jsonb)
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
    select max(updated) into last_attempt from public.salesquest_outbox
        where channel=p_channel and status in ('sending','sent','failed');
    if last_attempt is not null and ts-last_attempt<greatest(3,(limits->>'gapSec')::integer) then return null; end if;
    select * into item from public.salesquest_outbox where channel=p_channel and status='queued' and due<=ts
        order by due,id limit 1 for update skip locked;
    if item.id is null then return null; end if;
    update public.salesquest_outbox set status='sending',updated=ts where id=item.id;
    return to_jsonb(item);
end;
$$;

create function public.salesquest_claim_reminder()
returns boolean language plpgsql security invoker set search_path = '' as $$
declare last_try double precision; last_day text; ts double precision := extract(epoch from now());
begin
    perform pg_advisory_xact_lock(hashtext('salesquest:reminder'));
    select v into last_day from public.salesquest_kv where k='reminder_last';
    select v::double precision into last_try from public.salesquest_kv where k='reminder_try';
    if last_day=(now() at time zone 'Asia/Kolkata')::date::text or coalesce(last_try,0)>ts-3600 then return false; end if;
    insert into public.salesquest_kv(k,v) values('reminder_try',ts::text) on conflict(k) do update set v=excluded.v;
    return true;
end;
$$;

create function public.salesquest_poll(p_since bigint)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare current_ev bigint; events jsonb; cursor_ev bigint;
    day_start double precision := extract(epoch from ((now() at time zone 'Asia/Kolkata')::date::timestamp at time zone 'Asia/Kolkata'));
begin
    select v::bigint into current_ev from public.salesquest_kv where k='ev';
    select coalesce(jsonb_agg(to_jsonb(item)),'[]') into events from
        (select id,created,due,lead_id,channel,tpl_id,source,enroll_id,status,to_addr,error,sent_at,ev
         from public.salesquest_outbox where ev>p_since and ev<=current_ev order by ev limit 500) item;
    cursor_ev := case when jsonb_array_length(events)=500 then (events->499->>'ev')::bigint else current_ev end;
    return jsonb_build_object('ev',cursor_ev,'events',events,
        'queued',(select coalesce(jsonb_object_agg(channel,n),'{}') from
            (select channel,count(*) n from public.salesquest_outbox where status in ('queued','sending') group by channel) q),
        'failedToday',(select count(*) from public.salesquest_outbox where status='failed' and updated>=day_start),
        'sentToday',jsonb_build_object(
            'email',(select count(*) from public.salesquest_outbox where channel='email' and status='sent' and sent_at>=day_start),
            'whatsapp',(select count(*) from public.salesquest_outbox where channel='whatsapp' and status='sent' and sent_at>=day_start)),
        'recent',(select coalesce(jsonb_agg(to_jsonb(item)),'[]') from
            (select id,created,due,lead_id,channel,tpl_id,source,enroll_id,status,to_addr,error,sent_at,ev
             from public.salesquest_outbox order by id desc limit 60) item),
        'active',(select coalesce(jsonb_agg(to_jsonb(item)),'[]') from
            (select id,seq_id,lead_id,step,next_at from public.salesquest_enroll where status='active') item),
        'seqStats',(select coalesce(jsonb_object_agg(seq_id,stats),'{}') from
            (select seq_id,jsonb_object_agg(status,n) stats from
                (select seq_id,status,count(*) n from public.salesquest_enroll group by seq_id,status) totals group by seq_id) grouped));
end;
$$;

revoke all on function public.salesquest_record_event(), public.salesquest_stop_enrollments(bigint[]),
    public.salesquest_enroll_leads(text,text[]), public.salesquest_run_sequences(),
    public.salesquest_claim_message(text,jsonb), public.salesquest_claim_reminder(), public.salesquest_poll(bigint) from public, anon, authenticated;
grant execute on function public.salesquest_record_event(), public.salesquest_stop_enrollments(bigint[]),
    public.salesquest_enroll_leads(text,text[]), public.salesquest_run_sequences(),
    public.salesquest_claim_message(text,jsonb), public.salesquest_claim_reminder(), public.salesquest_poll(bigint) to service_role;
