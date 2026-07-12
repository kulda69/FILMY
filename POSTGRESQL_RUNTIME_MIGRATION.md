# PostgreSQL runtime vrstva

Šest malých zapisovaných tabulek lze tímto postupem zrcadlit z DuckDB do
PostgreSQL 14. Aplikace stále čte a zapisuje DuckDB; nejde o aplikační cutover.

## Rozsah

| Tabulka | DuckDB klíč | Další unikátní klíč |
| --- | --- | --- |
| `app.user_lists` | `id` | `slug` |
| `app.user_list_items` | `id` | `(list_id, canonical_key)` |
| `app.watch_events` | `id` | — |
| `app.user_ratings` | `canonical_key` | — |
| `app.content_state` | `tconst` | — |
| `app.user_people` | `person_key` | — |

PostgreSQL používá `text`, `bigint`, `integer`, `smallint`, `boolean`, `date`
a `timestamp without time zone` podle skutečných DuckDB sloupců. Záměrně nemá
FK do IMDb katalogu. `user_list_items.list_id` se při migraci kontroluje proti
`user_lists.id`, ale FK zatím nepřidáváme, aby schéma zůstalo věrné zdroji.

## Přístup

Runner vytváří dedikovanou roli `filmy_app` jako `LOGIN`, ale bez `INHERIT`,
členství v rolích, clusterových práv a `CREATE` na databázi či schématech. Má jen
`CONNECT`, `USAGE app` a DML na šesti runtime tabulkách. Heslo je pouze v
gitignored `.env` s právy `0600`. Runner před načtením hesel odmítne symlink,
cizího vlastníka i group/other oprávnění. App heslo nejprve nastaví v
PostgreSQL a až po úspěchu atomicky zapíše `.env`; první app endpoint přebírá
host a port administrátorského cíle. Existující app konfiguraci zachová jen
tehdy, pokud míří na tentýž server, databázi `filmy` a roli `filmy_app`.
Jde o least-privilege hranici uvnitř databáze `filmy`, ne o deklaraci úplné
izolace celého clusteru. Role může v jiné databázi zdědit například výchozí
`PUBLIC CONNECT`; skutečný přístup tam určují ACL dané databáze a `pg_hba.conf`.
Runner proto nemění ACL databází `postgres`, `skam` ani žádné jiné databáze.

## Opakované spuštění

Z rootu projektu:

```bash
uv run python -m filmy.scripts.bootstrap_postgresql
uv run python -m filmy.scripts.bootstrap_postgresql --check
uv run python -m filmy.scripts.migrate_runtime_to_postgresql
```

Prvni dva prikazy jsou povinne pred prvnim runtime behem: vedome aplikuji
`000_create_database.sql` a `001_bootstrap.sql` a overi jejich baseline. Runtime
runner je sam nikdy neaplikuje; hned na zacatku provede jen read-only bootstrap
check a pri chybejici nebo odlisne baseline skonci pred vytvorenim role a daty.

Runner:

1. read-only ověří předem aplikovaný bootstrap `000`/`001`;
2. vytvoří nebo zpřesní roli `filmy_app`;
3. před `002` povolí pouze prázdné `app` schema, nebo všech šest tabulek s už
   přesně shodným fingerprintem; částečný či cizí stav skončí bez změny;
4. aplikuje `002_runtime_schema.sql` bez grantů, ověří přesný fingerprint a
   teprve potom transakčně aplikuje idempotentní `003_runtime_grants.sql`;
5. ověří přesná ACL a přes app login provede DML smoke test s `ROLLBACK`;
6. teprve potom vyexportuje konzistentní read-only snapshot DuckDB do dočasných CSV;
7. načte CSV do PostgreSQL TEMP tabulek a v jedné transakci nahradí cílový obsah;
8. porovná počty a jeden deterministicky vybraný celý řádek každé neprázdné
   tabulky, zachycený ve stejné DuckDB transakci jako CSV;
9. zkontroluje `list_id` a znovu přesné schéma, ACL, roli a app DML smoke.

`003` dává DML výslovně pouze šesti uvedeným tabulkám. Nenastavuje default
privileges pro budoucí tabulky. Jakákoli další tabulka, sloupec, constraint,
RLS/policy, trigger, index, grant nebo členství role je odchylka a migrace skončí ještě
před `DELETE`/importem. `CREATE TABLE IF NOT EXISTS` proto slouží jen pro čisté
první vytvoření, ne k tichému opravování cizího schématu.

Výchozí běh neduplikuje data. Při chybě importní transakce se původní
PostgreSQL obsah zachová. DuckDB runner nikdy nemění.

Jen kontrola aktuálního stavu:

```bash
uv run python -m filmy.scripts.migrate_runtime_to_postgresql --check
uv run python -m filmy.scripts.bootstrap_postgresql --check
```

## Rollback

Aplikace je pořád na DuckDB, takže provozní rollback je pouze nepoužít
PostgreSQL zrcadlo. Není potřeba měnit kód ani data. Automatický `DROP` zde
záměrně není; tabulky lze ponechat pro audit nebo je později odstranit ručně po
nové záloze a výslovném rozhodnutí.

Pokud selže další pokus o migraci, transakce zachová poslední kompletní stav v
PostgreSQL. Autoritativní a runnerem pouze read-only otevíraný zdroj do cutoveru
zůstává `data/filmy.duckdb`. Tento krok aplikaci na PostgreSQL nepřepíná.
