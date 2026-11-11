-- Exact rights of the agents' runtime role. Run as the local DB administrator (the owner
-- of agents_student_kb), after schema.sql. The runtime role owns nothing and is no superuser.
DO $guard$ BEGIN
  IF current_database()<>'agents_student_kb' THEN
    RAISE EXCEPTION 'grants.sql only runs in agents_student_kb, not in %', current_database();
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='agents_student') THEN
    RAISE EXCEPTION 'Create the runtime role first: CREATE ROLE agents_student LOGIN';
  END IF;
  IF (SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname='agents_student') THEN
    RAISE EXCEPTION 'agents_student must not be a superuser or create roles or databases';
  END IF;
  IF (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database())='agents_student' THEN
    RAISE EXCEPTION 'agents_student must not own agents_student_kb';
  END IF;
END $guard$;
REVOKE ALL ON DATABASE agents_student_kb FROM PUBLIC;
GRANT CONNECT ON DATABASE agents_student_kb TO agents_student;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agents_student;
GRANT SELECT, INSERT, UPDATE ON team_kb_documents, team_kb_chunks TO agents_student;
GRANT USAGE, SELECT ON SEQUENCE team_kb_documents_id_seq, team_kb_chunks_id_seq TO agents_student;
