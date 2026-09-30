-- RAG de noticias CS2 — correr en el SQL editor del proyecto Supabase `cs-finance`.
-- pgvector vive en `extensions`, no en `public` (SEC-08: aviso extension_in_public).
create extension if not exists vector with schema extensions;

-- Instalaciones anteriores a SEC-08 la tienen en `public`: moverla.
do $$
begin
    if (select extnamespace::regnamespace::text from pg_extension where extname = 'vector') <> 'extensions' then
        alter extension vector set schema extensions;
    end if;
end $$;

create table if not exists public.rag_chunks (
    id           bigint generated always as identity primary key,
    source       text        not null,
    external_id  text        not null,
    chunk_index  int         not null default 0,
    title        text,
    url          text,
    content      text        not null,
    published_at timestamptz,
    embedding    extensions.vector(768) not null,
    created_at   timestamptz not null default now(),
    unique (external_id, chunk_index)
);

create index if not exists rag_chunks_embedding_idx
    on public.rag_chunks
    using hnsw (embedding extensions.vector_cosine_ops);

alter table public.rag_chunks enable row level security;

-- search_path vacío y nombres cualificados (SEC-08: function_search_path_mutable).
create or replace function public.match_rag_chunks(
    query_embedding extensions.vector(768),
    match_count int default 5
)
returns table (
    id bigint, source text, title text, url text,
    content text, published_at timestamptz, similarity float
)
language sql stable
set search_path = ''
as $$
    select c.id, c.source, c.title, c.url, c.content, c.published_at,
           1 - (c.embedding operator(extensions.<=>) query_embedding) as similarity
    from public.rag_chunks c
    order by c.embedding operator(extensions.<=>) query_embedding
    limit match_count;
$$;

-- Solo el backend (service_role) llama a la RPC. `public` incluido: anon lo heredaba de ahí.
revoke execute on function public.match_rag_chunks(extensions.vector, int) from public, anon, authenticated;
grant execute on function public.match_rag_chunks(extensions.vector, int) to service_role;
