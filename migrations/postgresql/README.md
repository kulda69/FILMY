# PostgreSQL bootstrap

Tyto skripty vytvori izolovanou databazi `filmy`, schemata `app` a `old`,
zapnou rozsireni `pg_trgm`, `unaccent` a `fuzzystrmatch` ve schematu `public` a odeberou vychozi opravneni `PUBLIC`
pro pripojeni i docasne tabulky v databazi, zapis do schematu `public` a vsechna prava ke
schematum `app` a `old`. Vlastnik databaze a PostgreSQL superuser zustavaji
administratory. `000` a `001` nevytvareji aplikacni role ani tabulky.
Navazujici runtime runner vytvori omezenou roli, `002_runtime_schema.sql`
vytvori sest explicitnich tabulek a az po exact fingerprint kontrole
`003_runtime_grants.sql` udeli aplikacni prava. Aplikace je PostgreSQL-only.

Bootstrap je fail-closed: prihlaseny administrator musi vlastnit databazi,
schemata `app`, `old` a `public` i obe rozsireni. Explicitni ACL smi mirit jen
na vlastnika. Jedinou povolenou vyjimkou je `PUBLIC USAGE` na schematu
`public` kvuli rozsireni; `PUBLIC` nema na databazi zadne pravo a nema `CREATE` ani
zadna prava na `app`/`old`. Neznamy grant cizi roli skript sam neodebere, ale
skonci s chybou, aby spravce zmenu nejprve vedome posoudil.

## Spusteni

Administratorske pripojeni se nacita z lokalniho `.env` pres promenne
`POSTGRES_ADMIN_HOST`, `POSTGRES_ADMIN_PORT`, `POSTGRES_ADMIN_DATABASE`,
`POSTGRES_ADMIN_USER` a `POSTGRES_ADMIN_PASSWORD`. Heslo se nepredava na
prikazove radce ani nevypisuje. Hodnoty v `.env` jsou autoritativni; runner
zamerne neprebira `PGOPTIONS`, `PGSERVICE`, `PGPASSFILE` ani jine `PG*`
promenne z okolniho procesu.

Klient `psql` se hleda v tomto poradi:

1. volitelna cesta `POSTGRES_PSQL_PATH` v `.env` (pokud je zadana chybne,
   runner skonci chybou a vedome nepouzije jiny klient),
2. PostgreSQL 14 v `/Library/PostgreSQL/14/bin/psql`,
3. klient pribaleny k pgAdmin 4,
4. `psql` dostupny v `PATH`.

```bash
uv run python -m filmy.scripts.bootstrap_postgresql
```

Prikaz lze spoustet opakovane. Existujici databazi ani objekty nemaze a znovu
aplikuje pozadovane nastaveni opravneni.

## Rucni kontrola

Po bootstrapu lze stav zkontrolovat stejnym runnerem. Kontrola skonci nenulovym
navratovym kodem, pokud nesedi cilova databaze a admin uzivatel, chybi nektere
schema nebo rozsireni, vlastnik databaze ci schemat neni prihlaseny admin, nebo
jsou prava `PUBLIC` prilis siroka:

```bash
uv run python -m filmy.scripts.bootstrap_postgresql --check
```

SQL soubory jsou rozdelene zamerne: `CREATE DATABASE` musi bezet nad jinou
databazi a mimo transakcni blok. Zmeny schemat, rozsireni a opravneni v
`001_bootstrap.sql` probiha v jedne transakci az po pripojeni do `filmy`.
Pokud uz nektery chraneny objekt existuje s jinym vlastnikem nebo obsahuje
neznamy explicitni grant, bootstrap skonci jasnou chybou a vlastnictvi ani ACL
sam neprepise. Pouze zname vychozi granty `PUBLIC CONNECT`, `PUBLIC TEMPORARY` a `PUBLIC CREATE`
jsou pri prvnim bootstrapu vedome odebrany.

Soubezne spusteni dvou prvnich bootstrapu muze zavodit mezi kontrolou existence
a `CREATE DATABASE` v `000_create_database.sql`. Runner je urcen pro jednorazove
administratorske spusteni; pri takovem zavodu staci po dokonceni jednoho procesu
prikaz zopakovat.

## Runtime schema 002

`002_runtime_schema.sql` je urcen pro cisty prvni beh nebo presne shodne schema
a neobsahuje zadne granty. Runner pred nim odmitne castecny nebo cizi stav,
po nem overi exact fingerprint a teprve pak spusti transakcni, idempotentni
`003_runtime_grants.sql`. Ten grantuje `filmy_app` jen `SELECT`, `INSERT`, `UPDATE` a `DELETE` na sesti
jmenovanych tabulkach; vlastnikem zustava prihlaseny administrator a `PUBLIC`
ani jina role nesmi mit zadny tabulkovy grant. Default ACL nesmi udelovat prava
`PUBLIC` ani `filmy_app`. Python runner po aplikaci, pred exportem/importem a po
importu porovnava presny fingerprint tabulek, vlastniku, sloupcu, defaultu,
RLS/policies, constraints, indexu a triggeru. Kontroluje take realne prihlaseni
`filmy_app` a DML smoke test v transakci ukoncene `ROLLBACK`. Odchylku sam
nereparuje a data pred kontrolou nemaze. Podrobny postup
je v `POSTGRESQL_RUNTIME_MIGRATION.md` v koreni projektu.

Omezeni role je garantovano uvnitr databaze `filmy`. Nejde o plnou izolaci
PostgreSQL clusteru: `PUBLIC CONNECT` nebo jina prava v databazich `postgres`,
`skam` a dalsich spravuji jejich vlastni ACL a `pg_hba.conf`; runtime migrace
jejich ACL zamerne nemeni.
