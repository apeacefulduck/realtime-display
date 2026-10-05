-- Only the backend service role can access encrypted Spotify session records.
create table public.homeflow_sessions (
    slot text primary key check (slot = 'spotify'),
    ciphertext text not null check (length(ciphertext) between 1 and 32768)
);
alter table public.homeflow_sessions enable row level security;
revoke all on table public.homeflow_sessions from public, anon, authenticated;
grant select, insert, update on table public.homeflow_sessions to service_role;
comment on table public.homeflow_sessions is 'HomeFlow server-only Fernet-encrypted Spotify authorization';
