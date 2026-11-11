-- Compatible core of the owner's Team KB. Run ONLY inside the student's new DB.
-- Refuses any other database: run as the local DB administrator, never as agents_student.
DO $guard$ BEGIN
  IF current_database()<>'agents_student_kb' THEN
    RAISE EXCEPTION 'schema.sql only runs in agents_student_kb, not in %', current_database();
  END IF;
END $guard$;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS team_kb_documents (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    source_path TEXT,
    title TEXT NOT NULL,
    agents TEXT[] NOT NULL DEFAULT '{}',
    scopes TEXT[] NOT NULL DEFAULT '{}',
    doc_type TEXT NOT NULL DEFAULT 'knowledge',
    status TEXT NOT NULL DEFAULT 'active',
    source_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('active','archived'))
);
CREATE TABLE IF NOT EXISTS team_kb_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES team_kb_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (
      to_tsvector('simple'::regconfig,coalesce(heading,'') || ' ' || content)
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id,chunk_index)
);
CREATE INDEX IF NOT EXISTS team_kb_documents_agents_gin ON team_kb_documents USING gin(agents);
CREATE INDEX IF NOT EXISTS team_kb_documents_scopes_gin ON team_kb_documents USING gin(scopes);
CREATE INDEX IF NOT EXISTS team_kb_chunks_tsv_gin ON team_kb_chunks USING gin(search_tsv);
CREATE INDEX IF NOT EXISTS team_kb_chunks_embedding_hnsw ON team_kb_chunks USING hnsw(embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS team_kb_migrations (
    id BIGSERIAL PRIMARY KEY,migration_name TEXT NOT NULL,source_root TEXT,
    documents_seen INTEGER NOT NULL DEFAULT 0,documents_written INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,documents_skipped INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
