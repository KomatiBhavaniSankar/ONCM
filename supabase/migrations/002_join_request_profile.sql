-- Upgrade an EXISTING database (created before the "Organizational Profile and Scale" form) - run once in the Supabase SQL Editor.
-- New installs do not need this: supabase/schema.sql already contains everything. The app also adds missing columns itself on start.
alter table public.join_requests
    add column if not exists org_type              varchar(40),
    add column if not exists registration_number   varchar(100),
    add column if not exists date_of_incorporation date,
    add column if not exists country               varchar(100),
    add column if not exists state_ut              varchar(100),
    add column if not exists city                  varchar(100),
    add column if not exists year_established      integer,
    add column if not exists registered_address    text,
    add column if not exists employee_range        varchar(20),
    add column if not exists geographic_presence   jsonb,
    add column if not exists beneficiary_segments  jsonb,
    add column if not exists standards_worked_with jsonb,
    add column if not exists certifications        text,
    add column if not exists carbon_services       jsonb,
    add column if not exists org_profile           text,
    add column if not exists website               varchar(300),
    add column if not exists contact_phone         varchar(40),
    add column if not exists contact_designation   varchar(120);

-- If the columns were created by the app as plain json, convert them to jsonb (no-op when already jsonb)
do $$
declare c text;
begin
    foreach c in array array['geographic_presence', 'beneficiary_segments', 'standards_worked_with', 'carbon_services']
    loop
        if (select data_type from information_schema.columns
            where table_schema = 'public' and table_name = 'join_requests' and column_name = c) = 'json' then
            execute format('alter table public.join_requests alter column %I type jsonb using %I::jsonb', c, c);
        end if;
    end loop;
end $$;

create index if not exists ix_join_requests_status     on public.join_requests (status, created_at desc);
create index if not exists ix_join_requests_email      on public.join_requests (lower(contact_email));
create index if not exists ix_join_requests_reg_number on public.join_requests (lower(registration_number));
create index if not exists ix_join_requests_org_name   on public.join_requests (lower(org_name));
-- Old rows have NULL in the new columns, which every CHECK below accepts. Copy the CHECK constraints from schema.sql (section 2)
-- if you also want the database itself to enforce the option lists.
