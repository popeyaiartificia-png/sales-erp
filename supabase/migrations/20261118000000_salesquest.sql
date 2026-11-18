-- Browser clients have no direct access. The cloud backend must verify the
-- signed-in user against salesquest_members before using its server-only key.
create table public.salesquest_members (
    user_id uuid primary key references auth.users(id) on delete cascade,
    role text not null default 'operator' check (role in ('admin', 'operator'))
);

create table public.salesquest_kv (
    k text primary key,
    v text not null
);

insert into public.salesquest_kv (k, v) values ('state_rev', '0'), ('ev', '0');

create table public.salesquest_outbox (
    id bigint generated always as identity primary key,
    created double precision not null,
    due double precision not null,
    lead_id text not null,
    channel text not null check (channel in ('email', 'whatsapp')),
    tpl_id text not null,
    source text,
    enroll_id bigint,
    status text not null check (status in ('queued', 'sending', 'sent', 'failed', 'skipped', 'cancelled')),
    to_addr text,
    subject text,
    body text,
    error text,
    sent_at double precision,
    provider_id text,
    ev bigint not null default 0,
    updated double precision not null
);

create table public.salesquest_enroll (
    id bigint generated always as identity primary key,
    seq_id text not null,
    lead_id text not null,
    started double precision not null,
    step integer not null default 0 check (step >= 0),
    next_at double precision not null,
    status text not null check (status in ('active', 'stopped', 'done')),
    reason text,
    updated double precision not null
);

alter table public.salesquest_outbox add constraint salesquest_outbox_enrollment
    foreign key (enroll_id) references public.salesquest_enroll(id);
create index salesquest_outbox_queue on public.salesquest_outbox (status, channel, due, id);
create index salesquest_outbox_events on public.salesquest_outbox (ev);
create index salesquest_enroll_due on public.salesquest_enroll (status, next_at);
create unique index salesquest_enroll_active on public.salesquest_enroll (seq_id, lead_id)
    where status = 'active';

alter table public.salesquest_members enable row level security;
alter table public.salesquest_kv enable row level security;
alter table public.salesquest_outbox enable row level security;
alter table public.salesquest_enroll enable row level security;

revoke all on public.salesquest_members, public.salesquest_kv,
    public.salesquest_outbox, public.salesquest_enroll from public, anon, authenticated;
grant select, insert, update, delete on public.salesquest_members, public.salesquest_kv,
    public.salesquest_outbox, public.salesquest_enroll to service_role;
revoke all on sequence public.salesquest_outbox_id_seq,
    public.salesquest_enroll_id_seq from public, anon, authenticated;
grant usage, select on sequence public.salesquest_outbox_id_seq,
    public.salesquest_enroll_id_seq to service_role;

-- The same row lock protects imports and browser saves from lost updates.
create function public.salesquest_save_state(p_rev bigint, p_state jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    current_rev bigint;
begin
    if p_state is null or jsonb_typeof(p_state) <> 'object'
        or jsonb_typeof(p_state -> 'leads') is distinct from 'array' then
        raise exception 'state must be an object with a leads list' using errcode = '22023';
    end if;
    select v::bigint into strict current_rev from public.salesquest_kv
        where k = 'state_rev' for update;
    if p_rev is distinct from current_rev then
        return jsonb_build_object('error', 'conflict', 'rev', current_rev);
    end if;
    insert into public.salesquest_kv (k, v) values ('state', p_state::text)
        on conflict (k) do update set v = excluded.v;
    update public.salesquest_kv set v = (current_rev + 1)::text where k = 'state_rev';
    return jsonb_build_object('rev', current_rev + 1);
end;
$$;

create function public.salesquest_import_state(p_state jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    current_rev bigint;
    existing_state text;
begin
    if p_state is null or jsonb_typeof(p_state) <> 'object'
        or jsonb_typeof(p_state -> 'leads') is distinct from 'array' then
        raise exception 'state must be an object with a leads list' using errcode = '22023';
    end if;
    select v::bigint into strict current_rev from public.salesquest_kv
        where k = 'state_rev' for update;
    select v into existing_state from public.salesquest_kv where k = 'state';
    if existing_state is not null then
        if existing_state::jsonb = p_state then
            return jsonb_build_object('rev', current_rev, 'already_imported', true,
                'leads', jsonb_array_length(p_state -> 'leads'));
        end if;
        raise exception 'Import refused: cloud ERP state already exists; no data was overwritten'
            using errcode = '23505';
    end if;
    insert into public.salesquest_kv (k, v) values ('state', p_state::text);
    update public.salesquest_kv set v = (current_rev + 1)::text where k = 'state_rev';
    return jsonb_build_object('rev', current_rev + 1, 'already_imported', false,
        'leads', jsonb_array_length(p_state -> 'leads'));
end;
$$;

revoke all on function public.salesquest_save_state(bigint, jsonb),
    public.salesquest_import_state(jsonb) from public, anon, authenticated;
grant execute on function public.salesquest_save_state(bigint, jsonb),
    public.salesquest_import_state(jsonb) to service_role;
