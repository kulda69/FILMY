-- Zaklad izolovane PostgreSQL databaze pro projekt FILMY.
-- Skript je idempotentni a zatim zamerne nevytvari zadne aplikacni tabulky.

BEGIN;

-- Bootstrap nesmi potichu prevzit cizi objekty ani odebrat nezname granty.
DO $$
DECLARE
    database_owner name;
    object_owner name;
    schema_owner name;
    schema_name text;
    extension_name text;
    extension_schema name;
    unexpected_acl text;
BEGIN
    SELECT pg_get_userbyid(datdba)
    INTO database_owner
    FROM pg_database
    WHERE datname = current_database();

    IF database_owner IS DISTINCT FROM current_user THEN
        RAISE EXCEPTION
            'Database % is owned by %, expected current administrator %',
            current_database(), database_owner, current_user;
    END IF;

    FOREACH schema_name IN ARRAY ARRAY['public', 'app', 'old'] LOOP
        SELECT pg_get_userbyid(nspowner)
        INTO schema_owner
        FROM pg_namespace
        WHERE nspname = schema_name;

        IF FOUND AND NOT (
            schema_owner IS NOT DISTINCT FROM current_user
            OR (schema_name = 'public' AND schema_owner = 'pg_database_owner')
        ) THEN
            RAISE EXCEPTION
                'Schema % is owned by %, expected current administrator % or pg_database_owner for public',
                schema_name, schema_owner, current_user;
        END IF;
    END LOOP;

    FOREACH extension_name IN ARRAY ARRAY['pg_trgm', 'unaccent', 'fuzzystrmatch'] LOOP
        SELECT pg_get_userbyid(extowner), namespace.nspname
        INTO object_owner, extension_schema
        FROM pg_extension AS extension_entry
        JOIN pg_namespace AS namespace
          ON namespace.oid = extension_entry.extnamespace
        WHERE extension_entry.extname = extension_name;

        IF FOUND AND (
            object_owner IS DISTINCT FROM current_user
            OR extension_schema IS DISTINCT FROM 'public'
        ) THEN
            RAISE EXCEPTION
                'Extension % is owned by % in schema %, expected administrator % and schema public',
                extension_name, object_owner, extension_schema, current_user;
        END IF;
    END LOOP;

    -- CONNECT, TEMPORARY a CREATE jsou jedine zname vychozi granty, ktere tento skript
    -- zamerne odebere. Jiny grant cizi roli se nesmi potichu "opravit".
    SELECT string_agg(
        format('%s:%s', COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'),
               privilege.privilege_type), ', ' ORDER BY privilege.grantee,
               privilege.privilege_type
    )
    INTO unexpected_acl
    FROM pg_database AS database_entry,
         LATERAL aclexplode(COALESCE(database_entry.datacl, '{}'::aclitem[])) AS privilege
    WHERE database_entry.datname = current_database()
      AND privilege.grantee <> database_entry.datdba
      AND NOT (
          privilege.grantee = 0
          AND privilege.privilege_type IN ('CONNECT', 'TEMPORARY')
      )
      AND NOT (
          pg_get_userbyid(privilege.grantee) = 'filmy_app'
          AND privilege.privilege_type = 'CONNECT'
      );

    IF unexpected_acl IS NOT NULL THEN
        RAISE EXCEPTION
            'Database % has unexpected explicit ACL grants: %',
            current_database(), unexpected_acl;
    END IF;

    SELECT string_agg(
        format('%s.%s:%s', schema_entry.nspname,
               COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'),
               privilege.privilege_type), ', ' ORDER BY schema_entry.nspname,
               privilege.grantee, privilege.privilege_type
    )
    INTO unexpected_acl
    FROM pg_namespace AS schema_entry,
         LATERAL aclexplode(COALESCE(schema_entry.nspacl, '{}'::aclitem[])) AS privilege
    WHERE schema_entry.nspname IN ('public', 'app', 'old')
      AND privilege.grantee <> schema_entry.nspowner
      AND NOT (
          schema_entry.nspname = 'public'
          AND privilege.grantee = 0
          AND privilege.privilege_type IN ('CREATE', 'USAGE')
      )
      AND NOT (
          schema_entry.nspname = 'app'
          AND pg_get_userbyid(privilege.grantee) = 'filmy_app'
          AND privilege.privilege_type = 'USAGE'
      );

    IF unexpected_acl IS NOT NULL THEN
        RAISE EXCEPTION 'Protected schemas have unexpected explicit ACL grants: %',
            unexpected_acl;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS old;

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch WITH SCHEMA public;

-- Vychozi PUBLIC opravneni jsou pro izolovanou aplikacni databazi prilis siroka.
-- Vlastnik databaze a PostgreSQL superuser si svuj administratorsky pristup drzi.
REVOKE CONNECT ON DATABASE filmy FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE filmy FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Finalni kontrola je soucasti transakce: pri odchylce se bootstrap vrati zpet.
DO $$
DECLARE
    problem text;
BEGIN
    WITH protected_schemas(name) AS (
        VALUES ('public'), ('app'), ('old')
    ), protected_extensions(name) AS (
        VALUES ('pg_trgm'), ('unaccent'), ('fuzzystrmatch')
    ), violations AS (
        SELECT format('database owner is %s', pg_get_userbyid(datdba)) AS detail
        FROM pg_database
        WHERE datname = current_database() AND datdba <> current_user::regrole
        UNION ALL
        SELECT format('schema %s is missing or owned by %s', expected.name,
                      COALESCE(pg_get_userbyid(namespace.nspowner), '<missing>'))
        FROM protected_schemas AS expected
        LEFT JOIN pg_namespace AS namespace ON namespace.nspname = expected.name
        WHERE namespace.oid IS NULL OR NOT (
            namespace.nspowner = current_user::regrole
            OR (expected.name = 'public' AND pg_get_userbyid(namespace.nspowner) = 'pg_database_owner')
        )
        UNION ALL
        SELECT format('extension %s is missing, owned by %s, or in schema %s',
                      expected.name, COALESCE(pg_get_userbyid(extension_entry.extowner), '<missing>'),
                      COALESCE(namespace.nspname, '<missing>'))
        FROM protected_extensions AS expected
        LEFT JOIN pg_extension AS extension_entry ON extension_entry.extname = expected.name
        LEFT JOIN pg_namespace AS namespace ON namespace.oid = extension_entry.extnamespace
        WHERE extension_entry.oid IS NULL
           OR extension_entry.extowner <> current_user::regrole
           OR namespace.nspname <> 'public'
        UNION ALL
        SELECT format('database ACL %s:%s is not allowed',
                      COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'),
                      privilege.privilege_type)
        FROM pg_database AS database_entry,
             LATERAL aclexplode(COALESCE(database_entry.datacl, '{}'::aclitem[])) AS privilege
        WHERE database_entry.datname = current_database()
          AND privilege.grantee <> database_entry.datdba
          AND NOT (
              pg_get_userbyid(privilege.grantee) = 'filmy_app'
              AND privilege.privilege_type = 'CONNECT'
          )
        UNION ALL
        SELECT format('schema ACL %s.%s:%s is not allowed', schema_entry.nspname,
                      COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'),
                      privilege.privilege_type)
        FROM pg_namespace AS schema_entry,
             LATERAL aclexplode(COALESCE(schema_entry.nspacl, '{}'::aclitem[])) AS privilege
        WHERE schema_entry.nspname IN ('public', 'app', 'old')
          AND privilege.grantee <> schema_entry.nspowner
          AND NOT (
              schema_entry.nspname = 'public'
              AND privilege.grantee = 0
              AND privilege.privilege_type = 'USAGE'
          )
          AND NOT (
              schema_entry.nspname = 'app'
              AND pg_get_userbyid(privilege.grantee) = 'filmy_app'
              AND privilege.privilege_type = 'USAGE'
          )
    )
    SELECT string_agg(detail, '; ' ORDER BY detail) INTO problem FROM violations;

    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'PostgreSQL bootstrap verification failed: %', problem;
    END IF;
END
$$;

COMMIT;
