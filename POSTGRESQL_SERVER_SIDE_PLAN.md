# PostgreSQL: plán rozumného přesunu logiky na server

Datum: 2026-07-13  
Stav: **návrh, nic z tohoto dokumentu zatím neimplementovat**

## Výsledek analýzy

Přechod na PostgreSQL je funkčně hotový, ale `filmy/runtime_postgres.py` má
zatím hlavně roli přepsaného repository modulu: každá Python funkce otevře
spojení a odešle SQL. To samo o sobě není chyba. PostgreSQL nemá být druhý
aplikační server a přesun SQL do funkce nezruší databázový round-trip.

Serverová logika má smysl jen tehdy, když splní alespoň jednu z těchto podmínek:

1. chrání invariant i při souběhu více requestů nebo background jobů;
2. z více zápisů dělá jednu atomickou doménovou akci;
3. nabízí opakovaně používaný datový pohled bez kopírování stejné SQL logiky;
4. materializuje skutečně drahou, pomalu se měnící projekci.

Podle toho jsou nejlepší první kandidáti importní commit a akce `Watched`.
Pythonové doporučování, TMDB/Plex HTTP komunikace a formátování pro Jinja
zůstávají v aplikaci.

## Co už PostgreSQL správně dělá

- `app.normalize_match_key()` a `app.alias_priority()` drží katalogovou
  normalizaci tam, kde patří.
- `app.latest_title_posters` a `app.catalog_title_cards` už jsou užitečné
  read views.
- Velké katalogové lookup vrstvy (`catalog_*`, `*_lookup`) jsou záměrně
  materializované tabulky při katalogovém refreshi. Nepřepisovat je na běžná
  views ani materialized views jen kvůli názvu objektu.
- Většina zápisů v `runtime_postgres.py` už používá jednu Python transakci,
  například TMDB detail + výměna providerů. To není potřeba mechanicky měnit
  na stored procedure.

## Prioritní přesuny

| Priorita | Funkčnost | Doporučený objekt | Proč |
| --- | --- | --- | --- |
| P0 | commit importního batch | SQL funkce `app.commit_import_batch(...)` + unikátní index | Dnes se resolved řádky čtou, kontrolují a zapisují po jednom. Mezi kontrolou a insertem je závod a jeden commit zbytečně otevírá mnoho transakcí. |
| P1 | `Watched` pro titul | SQL funkce `app.record_watched(...)` | Jedna doménová akce má atomicky vložit událost, aktualizovat `content_state` a provést výslovně požadovanou změnu seznamu. |
| P2 | společné projekce knihovny | 2 běžná `VIEW` | Stejné CTE pro převod epizody na seriál a pro agregaci watched stavu se opakuje v homepage, watch history i hot watchlistu. Získá se jedna definice, ne automatický výkonový zázrak. |
| P3 | snapshot ručních preferencí | jeden set-based SQL příkaz, případně funkce | `replace_favorite_genres/traits()` nyní provádí smyčku upsertů a další smyčku archivace. Data jsou malá, ale snapshot má být atomický. |
| P4 | stale `updated_at` | malý společný trigger, až po P0/P1 | Server má chránit čas změny u editovatelných tabulek. Nezavádět ho do importních/archivních tabulek bez jasné semantiky. |

### P0 — atomický commit importu

Vazba v kódu: `filmy/db.py:6060+`, `runtime_postgres.py` funkce
`fetch_resolved_import_rows`, `fetch_existing_import_commits`,
`insert_import_watch_event` a `mark_import_batch_committed`.

Návrh kontraktu:

```sql
app.commit_import_batch(p_batch_id text, p_committed_at timestamp)
  returns table(inserted_events integer, skipped_events integer);
```

Funkce má v jedné transakci:

1. zamknout batch (`FOR UPDATE`) a přijmout jen očekávaný stav;
2. vybrat pouze `resolution_status = 'resolved'` s `resolved_tconst`;
3. vložit chybějící watch events setově;
4. upsertovat `content_state` setově, s nejnovějším relevantním časem;
5. označit batch `committed`; vrátit počty.

Předtím přidat unikátní částečný index nad `(batch_id, import_row_id)` pro
ne-null hodnoty. To je skutečná ochrana proti duplicitě; samotná kontrola v
Pythonu jí není. Identifikátor importní události lze deterministicky odvodit
z batch a řádku, nebo je nutné nejdřív vědomě zavést databázový default pro
ID. Nevymýšlet generování ID přes náhodu skrytou ve funkci.

Přínos: správnost při retry/souběhu, jeden DB call a výrazně méně transakcí.

### P1 — `Watched` jako jedna serverová doménová akce

Vazba v kódu: `filmy/db_library.py:record_watch_event()`. Dnes vloží
`watch_events`, helper vloží/updatuje `content_state` a Python pak zvlášť
archivuje položku z watchlistu. Tento celek není atomický.

Návrh kontraktu:

```sql
app.record_watched(
  p_event_id text,
  p_tconst text,
  p_event_scope text,
  p_watched_on date,
  p_notes text,
  p_created_at timestamp,
  p_archive_from_list_id text default null,
  p_archive_canonical_key text default null
) returns table(event_id text, content_state_changed boolean, archived_items integer);
```

Funkce nesmí natvrdo předpokládat vždy `watchlist`. Volající má explicitně
předat seznam a canonical key, ze kterého se má položka archivovat; tím zůstane
budoucí pravidlo „Watched odebere z právě otevřeného aktivního seznamu“
vyjádřené v doméně, nikoli skryté v triggeru. Pro titul se archivace provede,
pro epizodu ne, pokud ji volající výslovně nevyžádá.

Do stejné migrace patří `CHECK` omezení alespoň pro `event_scope`,
`interest_state`, ratingy `1..10` a affinity `0..10`. Constraint je zde
lepší než trigger: je deklarativní, viditelný a funguje pro všechny klienty.

### P2 — dvě sdílené read views

Navržené views mají odstranit duplicitu, ne předčasně optimalizovat:

1. `app.active_user_list_display_items`
   - aktivní položky seznamů;
   - `display_tconst = COALESCE(series_tconst, item.tconst, parent_tconst)`;
   - ponechá data položky potřebná pro řazení a group operace.
2. `app.watched_display_rollup`
   - sjednocuje epizodu na seriál;
   - pro `display_tconst` poskytne `watch_count`, `latest_watched_on` a
     `latest_created_at`.

Po jejich zavedení se zjednoduší zejména `fetch_watch_view_page_rows`,
`fetch_hot_watchlist_page_rows`, `fetch_library_status_projection`,
`fetch_library_status_snapshot` a `fetch_user_list_page_rows`.

Běžné PostgreSQL view se obvykle inlineuje do výsledného plánu. Zrychlení se
neslibuje; kontroluje se pouze stejný výsledek a případná změna `EXPLAIN
(ANALYZE, BUFFERS)` na konkrétních homepage/list dotazech.

### P3 — preference jako setový snapshot

`replace_favorite_genres()` a `replace_favorite_traits()` mají stejný vzorec:
upsert doručených hodnot a archivaci chybějících. Implementovat jej nejprve
jako jeden parametrizovaný SQL statement s `jsonb_to_recordset`, případně pak
jako dvě malé SQL funkce. Nejde o výkon (desítky řádků), ale o to, aby mezi
upsertem a archivací nebyl vidět neúplný snapshot.

### P4 — `updated_at` trigger

Teprve až budou P0/P1 ověřené, lze přidat jednu funkci
`app.touch_updated_at()` a `BEFORE UPDATE` trigger na:

- `app.user_lists`, `app.user_list_items`, `app.user_ratings`,
  `app.user_people`, `app.favorite_genres`, `app.favorite_traits`;
- případně `app.content_state`, pokud se potvrdí, že timestamp má znamenat
  okamžik serverové změny, ne importovaný historický čas.

Trigger nemá nahrazovat doménovou logiku ani běžet nad `watch_events`, TMDB
historií a `old.*` archivem.

## Co zatím nepřesouvat

| Oblast | Rozhodnutí | Důvod |
| --- | --- | --- |
| `genre_scoring.py` a `suggestion_engine.py` | ponechat v Pythonu | Algoritmus je vysvětlitelný Python, často se bude měnit a kombinuje textové traits; PL/pgSQL by zhoršilo testování i čitelnost. SQL zůstane zdrojem vstupních řádků. |
| TMDB/Plex HTTP, filesystem assety, Jinja modely | ponechat v aplikaci | Jsou to externí I/O a prezentační odpovědnosti, nikoli databázové invarianty. |
| `store_tmdb_payload_bundle()` | zatím ponechat | Již je v jedné transakci; JSONB funkce by jen přesunula smyčku bez zásadního zisku. Znovu posoudit až při reálném bottlenecku. |
| move/copy/delete skupiny seznamu | nejdřív jedna Python transakce/set-based SQL | Správně jde o atomickou operaci, ale současný textový `id` nemá DB default a kontrakt práce s epizodovou skupinou se ještě vyvíjí. Funkce až po stabilizaci pravidel. |
| katalogové `*_lookup` tabulky | ponechat jako dnešní refreshované tabulky | Jsou to už materiálizované a indexované projekce velkého katalogu. |
| stored procedures s `CALL` | nepoužívat pro runtime | Psycopg a web requesty přirozeně řídí transakci; SQL funkce jsou pro P0/P1 vhodnější. Procedure má smysl až pro vědomý administrační maintenance job. |
| obecný trigger „po každém watch eventu“ | nepoužívat | Skryl by pravidla importu, epizod a archivace listu a hůře by se testoval. Explicitní funkce je čitelnější. |

## Materialized views: pouze po měření

Nejrozumnější experiment je `app.catalog_genre_counts`: dnešní
`fetch_catalog_genres()` při každém volání rozděluje `genres` přes celý katalog.
Výsledek je malý a mění se jen při katalogovém refreshi. Pokud měření ukáže
náklad, materialized view se obnoví na konci katalogového rebuild procesu.

Naopak z `app.catalog_title_cards` ani `app.latest_title_posters` nyní
materialized view nedělat. První by kopíroval přes milion titulů a druhý je
spíš kandidát na cílený index a měření. Nejdříve změřit a případně zkusit index
`app.tmdb_assets (tconst, asset_kind, fetched_at DESC, id DESC)` s podmínkou
pro fetched postery.

## Implementační pořadí pro další model

1. **Baseline:** uložit `EXPLAIN (ANALYZE, BUFFERS)` pro import commit,
   homepage/list dotazy a zaznamenat funkční očekávání testy. Ověřit živé
   schema přes `/Library/PostgreSQL/14/bin/psql` nebo existující bootstrap
   helper; `psql` není v běžném `PATH`.
2. **Migrace P0:** nová číslovaná SQL migrace, unikátní index, constraints a
   `app.commit_import_batch`. Upravit granty a offline schema fingerprint
   testy; přepsat Python na jediný call.
3. **Test P0:** opakovaný commit stejného batch, dva souběžné commity, rollback
   při chybě a přesné počty eventů/content state. Žádná duplicita.
4. **Migrace P1:** `app.record_watched` s explicitním seznamem k archivaci.
   Python ponechat pro validaci detailu/identity a invalidaci presentation
   cache, ale DB mutation nahradit jedním callem.
5. **Test P1:** titul, epizoda, chyba uprostřed operace, watchlist i jiný
   aktivní seznam; zkontrolovat, že event, state a list jsou konzistentní.
6. **P2 views:** přidat po jedné, nejdřív stejné výsledky na fixture i reálném
   anonymizovaném výběru; až pak nahradit opakovaná CTE.
7. **Měření a rozhodnutí:** teprve podle výsledků rozhodnout o
   `catalog_genre_counts` materialized view a P3/P4. Každou změnu držet jako
   samostatnou migraci a samostatný commit.

## Oprávnění a migrace

`filmy_app` dnes dostává přímé DML na runtime tabulky. P0/P1 lze spustit jako
`SECURITY INVOKER`, takže nejde o bezpečnostní změnu. Teprve pokud bude cílem
omezit roli jen na doménové akce, je nutná oddělená bezpečnostní fáze:

1. `SECURITY DEFINER` funkce s pevně nastaveným `search_path`;
2. revize všech potřebných read/write kontraktů;
3. odebrání přímých DML grantů až po úplném pokrytí;
4. test ACL pod skutečným `filmy_app`.

Nespojovat to s P0/P1. Nejprve správnost a srozumitelnost, pak případné
zpevnění práv.

## Akceptační kritéria

- veřejné FastAPI kontrakty a návratové tvary se nezmění;
- žádný nový background job ani server se kvůli této práci nespouští;
- všechny nové serverové objekty jsou verzované migrací, granty a testy;
- P0 a P1 prokazatelně přežijí retry/souběh bez částečného stavu;
- pro views se porovnává výsledek před/po a výkon se tvrzeně neodvozuje bez
  `EXPLAIN ANALYZE`;
- DuckDB rollback/rebuild vrstva v `filmy/db_bootstrap.py` zůstává nedotčená.
