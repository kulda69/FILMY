# Audit přechodu z DuckDB na PostgreSQL

Datum: 2026-07-10

## Výsledek

PostgreSQL dává pro živou část aplikace smysl, ale úplný jednorázový přepis celé
databáze teď není bezpečný první krok. Doporučený postup je nejdřív oddělit
zapisovaná aplikační data od velkého IMDb katalogu:

- PostgreSQL jako cílová databáze pro uživatelská data, TMDB stav, importní stav
  a background joby.
- DuckDB dočasně ponechat jako read-only katalogovou vrstvu pro IMDb data a
  existující lookup tabulky.
- O úplném přesunu katalogu rozhodnout až podle měření hybridní varianty a
  prototypu vyhledávání v PostgreSQL.

To řeší hlavní současný problém — souběžné zápisy — bez okamžitého přepisu
celého importu, vyhledávání a přibližně 100 milionů materializovaných řádků.

## Ověřená výchozí situace

- Aktivní `data/filmy.duckdb` má 6,3 GiB.
- V databázi je 28 tabulek ve schématu `app`, 16 archivních tabulek ve schématu
  `old` a 7 `raw` views nad IMDb TSV soubory.
- Největší materializované vrstvy:
  - `app.title_aliases`: 58 172 355 řádků
  - `app.title_credits`: 14 300 072 řádků
  - `app.catalog_episodes`: 9 742 325 řádků
  - `app.title_alias_lookup`: 7 246 857 řádků
  - `app.catalog_people` a `app.person_lookup`: každá 3 407 896 řádků
  - `app.catalog_titles` a `app.title_lookup`: každá 1 265 895 řádků
- Živá osobní data jsou proti katalogu malá: `watch_events` má 21 108 řádků,
  `user_list_items` 1 187 a `user_ratings` 252.
- Přímé `duckdb.connect(...)` se používá na 49 místech. Sdílené helpery
  `_run_duckdb_read()` a `_run_duckdb_write()` už existují, ale část kódu je
  stále obchází.
- Na Macu běží PostgreSQL 14.12 z instalace v `/Library/PostgreSQL/14`.
  Hlavní proces je `/Library/PostgreSQL/14/bin/postmaster` s datovým adresářem
  `/Library/PostgreSQL/14/data`; PID 630 je jeho `walwriter` proces.
- Binárky této instalace nejsou v běžném shellovém `PATH`, proto původní
  kontrola přes `command -v psql` a `command -v pg_isready` chybně vypadala
  jako nenainstalovaný PostgreSQL. PgAdmin navíc obsahuje vlastní klient
  PostgreSQL 18.4.
- Server přijímá spojení přes Unix socket `/private/tmp/.s.PGSQL.5432` na portu
  5432. TCP připojení na `127.0.0.1:5432` neodpovědělo, takže pro prototyp je
  potřeba použít socket nebo vědomě upravit síťovou konfiguraci. Přístupové
  údaje a vhodnost této instance pro FILMY ještě ověřené nejsou.
- Bezpečnostní kopie `/Volumes/not_inserted/PycharmProjects/FILMY_copy` byla
  ověřena proti pracovnímu projektu pomocí `rsync --dry-run`. Aplikační soubory
  a data se neliší; rozdíl je jen ve dvou PyCharm metadata souborech.

## Co není přenositelné pouhou výměnou driveru

### Import IMDb katalogu

DuckDB dnes čte TSV přímo přes `read_csv_auto(...)` a vytváří nad nimi `raw`
views. PostgreSQL potřebuje jiný importní krok, typicky staging tabulky a `COPY`,
případně export z DuckDB do Parquet/CSV a následný bulk import.

### Odvozené tabulky a vyhledávání

Katalogový refresh používá mimo jiné:

- `CREATE OR REPLACE TABLE ... AS`
- `unnest(string_split(...))`
- `TRY_CAST`
- DuckDB variantu `regexp_extract`
- `levenshtein(...)`
- rozsáhlé lookup tabulky s prefixovými indexy

PostgreSQL má pro většinu z toho ekvivalent, ale SQL se musí přepsat. Pro fuzzy
hledání bude vhodné prověřit rozšíření `pg_trgm` a případně `unaccent`; současné
prefixové lookup tabulky se nesmí slepě převzít bez měření.

### Připojení a transakce

Současné helpery otevírají nové souborové připojení pro jednotlivou akci a při
locku opakují celý callback. V PostgreSQL má být místo toho:

- connection pool,
- explicitní transakční hranice,
- retry jen pro vybrané přechodné chyby,
- jedna konfigurovatelná databázová vrstva pro web i background procesy.

## Doporučený migrační sled

### Fáze 0 — hotovo: návratový bod

- Zachovat aktuální `FILMY_copy` beze změn.
- Před každým skutečným cutoverem znovu ověřit kopii databáze a export
  uživatelských tabulek.

### Fáze 1 — databázová hranice bez změny chování

- Zavést malý modul pro otevírání read/write spojení a transakce.
- Přestat z aplikačních modulů volat `duckdb.connect(...)` přímo.
- Oddělit repository funkce pro katalogová čtení a pro aplikační zápisy.
- Zachovat současné veřejné funkce `filmy.db` jako fasádu, aby se neměnily routy
  a šablony zároveň s databází.

### Fáze 2 — prototyp nad existujícím PostgreSQL

- Neinstalovat další PostgreSQL naslepo. Nejdřív ověřit přístupové údaje
  existující instalace 14.12 a zda má tato instance sloužit i pro FILMY.
- Přidat driver a pool; pro jednoduchý synchronní kód je vhodný `psycopg` s
  poolem. Není nutné zavádět ORM.
- Vytvořit schémata `app` a `old` a migrační skripty verzované v repozitáři.
- Přenést malou reprezentativní množinu: `user_lists`, `user_list_items`,
  `watch_events`, `user_ratings`, `content_state` a `user_people`.
- Ověřit atomické akce `Watched`, přesuny/kopie mezi listy a souběh webu s
  background zápisem.

### Fáze 3 — runtime cutover

- Přenést zbytek zapisovaných `app` tabulek: TMDB mapování/detail/provider stav,
  importní stav, preference, genre scores a search recall.
- Udělat jednorázový export a import s kontrolními počty a několika konkrétními
  `tconst`/`nconst` záznamy.
- Přepnout zápisy na PostgreSQL. DuckDB v této fázi zůstane jen read-only pro
  IMDb katalog a katalogové lookupy.
- Po stabilizační době odstranit dualní fallback, ne dřív.

### Fáze 4 — rozhodnutí o katalogu

Porovnat dvě varianty na reálných dotazech:

1. Hybrid: PostgreSQL pro runtime data, DuckDB pro IMDb katalog.
2. Plný PostgreSQL: katalog importovaný přes bulk `COPY`, hledání přes cílené
   indexy a `pg_trgm`.

Plný přesun má pokračovat jen tehdy, pokud odstraní provozní složitost a zároveň
udrží nebo zlepší časy homepage, search a detailu. Samotná touha mít jednu
databázi není dostatečný důvod k migraci téměř 100 milionů řádků.

## Kontrolní podmínky pro každý cutover

- Počty řádků zdroj/cíl pro každou migrovanou tabulku.
- Kontrola vazeb na vybraných filmech, seriálech, epizodách a osobách.
- Stejný výsledek pro watchlist, watched history, ratingy a user listy.
- Žádný zápis do staré databáze po přepnutí dané domény.
- Měřené časy `/`, `/search` a `/titles/{tconst}` před a po změně.
- Ověřený rollback na předchozí konfiguraci bez zpětné ztráty uživatelských dat.

## Nejbližší implementační krok

První inkrement Fáze 1 je hotový: `filmy/database.py` centralizuje otevírání
DuckDB spojení a retry mechanismus a `filmy/db_library.py` už nepoužívá přímé
interní připojení přes `filmy.db`.

Nevytvářet zatím PostgreSQL databázi pro FILMY a nemigrovat data. Další malý
krok je převést na stejnou hranici `filmy/db_people.py` a potom pojmenovat
samostatné repository rozhraní pro runtime data. Tím vznikne skutečný přepínací
bod a zároveň se zmenší dnešní DuckDB lock dluh i v případě, že se migrace
později zastaví.
