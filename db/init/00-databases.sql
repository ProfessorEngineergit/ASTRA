-- Runs once on first Postgres init (against POSTGRES_DB).
-- Creates the separate databases used by the stack.
CREATE DATABASE cortex;
CREATE DATABASE n8n;
CREATE DATABASE langfuse;
