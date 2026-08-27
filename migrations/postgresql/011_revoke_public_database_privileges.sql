-- Srovna legacy databaze s bezpecnym bootstrapem bez zmeny dat nebo roli.
REVOKE CONNECT ON DATABASE filmy FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE filmy FROM PUBLIC;
