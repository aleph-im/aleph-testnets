-- Create the scheduler database if it doesn't already exist.
-- Mounted into /docker-entrypoint-initdb.d/ so Postgres runs it on first start.
SELECT 'CREATE DATABASE scheduler'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'scheduler')\gexec
