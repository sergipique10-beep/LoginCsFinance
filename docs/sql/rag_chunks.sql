-- RAG de noticias CS2 — correr en el SQL editor del proyecto Supabase `cs-finance`.
create extension if not exists vector;

create table if not exists public.rag_chunks (
    id           bigint generated always as identity primary key,
    source       text        not null,
    external_id  text        not null,
    chunk_index  int         not null default 0,
    title        text,
    url          text,
    content      text        not null,
    published_at timestamptz,
    embedding    vector(768) not null,
    created_at   timestamptz not null default now(),
    unique (external_id, chunk_index)
);

create index if not exists rag_chunks_embedding_idx
    on public.rag_chunks
    using hnsw (embedding vector_cosine_ops);

alter table public.rag_chunks enable row level security;

create or replace function public.match_rag_chunks(
    query_embedding vector(768),
    match_count int default 5
)
returns table (
    id bigint, source text, title text, url text,
    content text, published_at timestamptz, similarity float
)
language sql stable as $$
    select c.id, c.source, c.title, c.url, c.content, c.published_at,
           1 - (c.embedding <=> query_embedding) as similarity
    from public.rag_chunks c
    order by c.embedding <=> query_embedding
    limit match_count;
$$;
