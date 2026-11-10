-- Sets the runtime role's password from the AGENTS_KB_PASSWORD environment variable that
-- student_tools.py vault-run fetched from the student's Bitwarden. The value never becomes
-- a command-line argument or console output. Run as the local DB administrator:
--   PYTHON student_tools.py --config SETTINGS vault-run -- psql -X -q -v ON_ERROR_STOP=1 -d postgres -f set-role-password.sql
\set QUIET on
\set ECHO none
\set pw `printenv AGENTS_KB_PASSWORD`
SELECT length(:'pw') >= 16 AS password_long_enough \gset
\if :password_long_enough
  ALTER ROLE agents_student WITH LOGIN PASSWORD :'pw';
  \echo 'agents_student password set (value not shown)'
\else
  DO $stop$ BEGIN RAISE EXCEPTION 'AGENTS_KB_PASSWORD is missing or shorter than 16 characters; nothing changed'; END $stop$;
\endif
\unset pw
