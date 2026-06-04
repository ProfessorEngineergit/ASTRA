-- Load the cortex schema into the cortex database on first init.
-- (./db is mounted read-only at /db in the postgres container.)
\connect cortex
\i /db/schema.sql
