-- Spousti se pres psql nad administracni databazi (obvykle postgres).
-- \gexec zajisti, ze CREATE DATABASE probehne jen tehdy, kdyz cil neexistuje.
SELECT 'CREATE DATABASE filmy'
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_database
    WHERE datname = 'filmy'
) \gexec
