# Historie projektu

## 2026-07-13 - Analýza serverové logiky PostgreSQL

- Vznikl návrh [POSTGRESQL_SERVER_SIDE_PLAN.md](POSTGRESQL_SERVER_SIDE_PLAN.md) pro cílený přesun logiky z `filmy/runtime_postgres.py` na PostgreSQL.
- První kandidáti nejsou plošné stored procedures: prioritu má atomický commit importního batch a následně explicitní doménová funkce `Watched`.
- Opakované read CTE jsou kandidát na dvě běžná views; materialized view jen po měření, nejvýše pro katalogové počty žánrů.
- Doporučovací scoring, externí integrace a Jinja modely zůstávají v Pythonu.

## 2026-07-13 - Aktivace Project Brain a uzavření PG-first runtime řezu

- Project Brain byl aktivován se stabilním ID `github.com/kulda69/filmy`.
- Obnoven kontext relace `019f4c3f-d1c9-7b80-8ac0-b749e966f038`; otevřeným bodem bylo dočištění posledních šesti explicitních DuckDB spojení v `filmy/db.py`.
- Bootstrap a vědomý rollback DuckDB jsou nyní izolované v `filmy/db_bootstrap.py`; hlavní runtime modul už DuckDB spojení přímo neotevírá.
- Testy byly aktualizovány na skutečný PostgreSQL kontrakt. Celá sada 46 testů i PG-only smoke s vypnutým `duckdb.connect` prošly.

## 2026-07-13 - Zúžení title detail double-build a odlehčení main-cast partialu

- Partial routa `/titles/{tconst}/main-cast` už neskládá celé `get_title_presentation(tconst)`, ale používá nový lehký helper `get_title_people_panel()` jen pro credits blok.
- `get_title_presentation()` bylo rozšířeno o detailová pole potřebná přímo pro title page (`content_state`, `tmdb_details`, `tmdb_providers`, `backdrop_url`), takže routa `/titles/{tconst}` už vedle presentation cache znovu nevolá `get_content_detail()`.
- Cache verze title presentation se zvedla na `3`, aby se starší soubory s neúplným payloadem nepoužívaly jako falešně validní hit.
- Cílený helper smoke nad `tt0133093` prošel: presentation vrací nové detailové klíče a people panel vrací očekávaný cast/director blok.

Související rozhodnutí: [DuckDB zůstává pouze jako izolovaná údržbová a rollback vrstva](Rozhodnuti%20projektu.md#2026-07-13---duckdb-zůstává-pouze-jako-izolovaná-údržbová-a-rollback-vrstva)

## 2026-07-18 - Navázání na výkonový cleanup a homepage smoke

- Živý FastAPI smoke na `127.0.0.1:8019` prošel pro domovku, title detail, `main-cast` partial, suggestion overview/detail routy a people search.
- `main-cast` partial i title detail cache hit jsou po předchozím řezu potvrzené jako rychlé v HTTP vrstvě; další reálný kandidát zůstala domovka a listový panel.
- `fetch_user_list_page_rows()` v PostgreSQL runtime byl zjednodušen tak, aby běžná stránka brala položky i filtrovaný total jedním dotazem přes `COUNT(*) OVER ()`; samostatný count zůstává jen pro out-of-range offset.
- Ověření nad `watchlist` potvrdilo stejný total `480`, správných 50 položek na běžných stránkách a korektní prázdnou stránku při offsetu mimo rozsah.

## 2026-07-18 - První backend řez AI taste bridge

- Podle poznámky [AI_propojeni.md](AI_propojeni.md) vznikl první lokální kontrakt pro předání vkusu samostatné AI vrstvě bez přímé integrace placeného ChatGPT API.
- `app.user_ratings` v PostgreSQL bylo rozšířeno o `liked_notes` a `disliked_notes`; stejná pole jsou doplněná i v runtime schématu, migrátorovém verifieru a DuckDB fallback definici.
- API zápis ratingu `POST /api/library/content/{tconst}/rating` umí přijmout slovní plus/mínus poznámky.
- Přibyl read-only endpoint `GET /api/ai/taste-seed`, který pro zvolený seznam vrací JSON s IMDb/TMDB identitou, názvy, žánry, IMDb a lokálním ratingem, slovními poznámkami a základními scoring/affinity signály.
- Skutečný seznam má aktuálně slug `kouknout-znou`, ale endpoint umí uživatelský alias `kouknout-znovu`, aby odpovídal zamýšlenému názvu „Kouknout znovu“.
- Veřejné/navazující endpointy se nově zapisují do [API_ENDPOINTY.md](API_ENDPOINTY.md), aby měl projekt `filmy-knihy` samostatný kontrakt bez čtení interního kódu.
- `pytest` byl přidaný jako dev dependency přes `uv add --dev pytest`; `pyproject.toml` dostal minimální konfiguraci `pythonpath = ["."]`.
- Širší cílený `pytest` běh nejdřív odhalil, že stará DuckDB kopie nemá nové sloupce `liked_notes` / `disliked_notes`; migrátor proto při exportu z historické DuckDB doplňuje pro tyto známé nové sloupce `NULL`.
- Ověření: dočasný rating upsert s poznámkami prošel a byl uklizen, `compileall` prošel, HTTP smoke endpointu na `127.0.0.1:8055` vrátil `200` a cílená pytest sada prošla `41 passed`.

Související rozhodnutí: [AI taste bridge je datový kontrakt, ne náhrada lokálního scoringu](Rozhodnuti%20projektu.md#2026-07-18---ai-taste-bridge-je-datový-kontrakt-ne-náhrada-lokálního-scoringu)

## 2026-07-18 - UI pro slovní hodnocení v detailu titulu

- Detail titulu dostal v existujícím menu novou položku `Přidat slovní hodnocení` / `Upravit slovní hodnocení`.
- Formulář ukládá `liked_notes` a `disliked_notes` přes nový UI POST `/ui/list-actions/rating-notes`; při existujícím číselném ratingu ho zachová, při chybějícím ratingu si ho vyžádá.
- `library_state.rating` v PostgreSQL runtime nyní nese i slovní poznámky, aby je detail titulu mohl zobrazit bez zvláštního dotazu.
- Slovní hodnocení je pevná sekce pod people/Main cast blokem a nezávisí na existenci `Aliases`; zobrazuje dvě karty `Klady` a `Zápory` i ve chvíli, kdy jsou texty zatím prázdné.
- Ověření: `compileall`, `uv run pytest tests/test_runtime_postgres_content_state.py` a HTTP render `/titles/tt0242795` na `127.0.0.1:8056` i `/titles/tt5464086` na `127.0.0.1:8057` prošly.
- Následně se ukázalo, že uložené slovní hodnocení pro `tt5464086` detail pořád nezobrazuje. Data v PostgreSQL byla správně; chyběl průchod přes `fetch_library_summary_snapshot()`, který vracel jen `rating` a `rated_at`.
- Helper byl rozšířen o `liked_notes` a `disliked_notes`; `get_content_detail('tt5464086')` i HTTP render na `127.0.0.1:8058` pak potvrdily oba uložené texty. Cílený pytest prošel `11 passed`.
- Slovní hodnocení se nově zobrazuje jako bezpečný lokální Markdown subset. Raw HTML se escapuje, podporované jsou odstavce, odrážky/číslované seznamy, tučné, kurzíva, inline code, odkazy a jednoduché nadpisy. Ověření: nový `tests/test_markdown_rendering.py`, cílený pytest prošel `13 passed` a HTTP render `/titles/tt5464086` na `127.0.0.1:8059` prošel.

## 2026-07-18 - Implementace AI context endpointu

- Přibyl read-only endpoint `GET /api/ai/context` pro navazující AI projekt.
- Endpoint vrací stabilní ratingové škály včetně minim a maxim, celé `Favorite Genres`, celé `Favorite Traits`, poznámky ke scoring signálům a usage notes.
- `API_ENDPOINTY.md` byl aktualizován ze stavu `planovano` na `implementovano`, aby zůstal živým kontraktem pro projekt `filmy-knihy`.
- Ověření: `compileall` prošel, cílený pytest `tests/test_app_state_postgres_overlay.py tests/test_api_ai_context.py` prošel `6 passed` a lokální volání helperu vrátilo aktuální JSON s reálnými preferencemi.

## 2026-07-19 - People affinity v AI taste seed kontraktu

- `GET /api/ai/taste-seed` nově vrací u každé položky `people_affinity`: konkrétní ručně hodnocené osoby z kreditu titulu včetně `nconst`, jména, credit group, pořadí, affinity ratingu a favorite flagu.
- Souhrnné `actor_affinity_rating` zůstává vedle toho zachované jako agregovaný signál z hlavního obsazení, aby navazující AI vrstva měla jak skóre, tak vysvětlitelné vazby titul -> osoba.
- `API_ENDPOINTY.md` byl upraven tak, aby už `people_affinity` nepopisoval jako plánované omezení.
- Ověření: `compileall`, cílený pytest `tests/test_api_ai_context.py tests/test_app_state_postgres_overlay.py` prošel `7 passed` a živé volání `get_ai_taste_seed(limit=50)` našlo položku s vyplněným `people_affinity`.

## 2026-07-19 - Databázové role seznamů pro AI tipy

- `app.user_lists` má nový sloupec `ai_input_role`, který říká, jak se seznam smí používat pro AI tipy.
- Povolené hodnoty jsou `strong_positive`, `interested_owned`, `interested_planned`, `in_progress`, `negative`, `external_suggestion` a `ignore`.
- Současné seznamy byly databázově označené: `Kouknout znou` jako silně pozitivní příklady, `Mam` jako slabší pozitivní signál vlastnictví/stažení, běžné plánovací seznamy jako zájem, `Nedokoukáno` jako negativní signál a `Plex Library` jako ignorovat.
- Vznikl smazatelný custom seznam `AI návrhy` (`ai-navrhy`) s rolí `external_suggestion`; je určený jako schránka návrhů od AI a nesmí se používat jako vstup pro další AI doporučování.
- Ověření: runtime schema bylo aplikováno a ověřeno verifierem, cílený pytest `tests/test_postgresql_migration_offline.py tests/test_user_lists_postgres_overlay.py` prošel `23 passed`, `compileall` prošel a živý helper `fetch_user_lists()` vrací nové role.

## 2026-07-19 - Budoucí import AI doporučení musí počítat s chudým JSON a duplicitami

- Import návrhů od AI nesmí předpokládat, že AI vrátí kompletní metadata titulu. Minimální praktický vstup může být jen název, rok a případně IMDb `tt...`; preferovaný kontrakt má AI nutit dodat IMDb identifikaci, pokud ji umí.
- Resolver v aplikaci musí umět návrh napárovat na interní IMDb `tconst`, případně držet nerozlišený návrh jako stav k ručnímu dořešení.
- Opakované vlny AI doporučení mohou vracet stejné tituly. Budoucí model má proto evidovat doporučovací běhy/vlny a deduplikovat podle vyřešeného `tconst`, případně podle normalizovaného nevyřešeného názvu+roku.
- `AI návrhy` je cílový seznam pro kandidáty, ale samotná evidence doporučení má držet i původ, běh, zdůvodnění a počet výskytů, aby se neztratilo, proč se titul objevil opakovaně.

## 2026-07-19 - UI editace role seznamu Pro AI tipy

- Editace seznamu na domovce i na detailu seznamu má nový dropdown `Pro AI tipy`.
- Dropdown ukládá `app.user_lists.ai_input_role`, takže Jiří může později přepnout, zda seznam znamená silně pozitivní příklady, stažené/zajímavé tituly, negativní signál, návrh od AI nebo ignorovat.
- Aplikační vrstva validuje stejnou pevnou sadu hodnot jako PostgreSQL constraint, aby se do DB nedostaly neznámé role.
- Ověření: cílený pytest `tests/test_user_lists_postgres_overlay.py tests/test_postgresql_migration_offline.py` prošel `25 passed`, `compileall` prošel a FastAPI render smoke pro `/?list_id=ai-suggestions` i `/lists/ai-suggestions` vrátil HTML s dropdownem.

## 2026-07-19 - Budoucí využití AI návrhů na domovce

- Sekce `Continue Watching` zatím nemá pro Jiřího praktické využití.
- Až bude seznam `AI návrhy` reálně naplněný doporučeními, má smysl zobrazit několik těchto položek na domovce právě v prostoru dnešní `Continue Watching` sekce.
- Změna nemá proběhnout dřív, než existuje import/naplnění AI návrhů a jasný fallback pro prázdný seznam.

## 2026-07-19 - Databázový základ signálů role/postavy v titulu

- Vznikla PostgreSQL tabulka `app.user_title_role_signals` pro signály typu „v tomhle titulu mě zaujala tahle role/postava“, oddělené od celkového hodnocení titulu i globální obliby herce.
- Model ukládá `tconst`, volitelné `nconst`, jméno postavy, typ signálu (`character`, `dialogue`, `behavior`, `relationship_dynamic`, `performance`, `visual_appeal`, `attraction`, `other`), polaritu, sílu 0-10 a poznámku.
- Aplikační fasáda `set_title_role_signal()` validuje existenci titulu/osoby a skládá stabilní `signal_key`, takže opakované uložení stejného signálu aktualizuje původní řádek.
- `get_title_role_signals()` vrací signály pro jeden titul a je připravené pro pozdější detail UI i AI endpointy.
- Ověření: runtime schema bylo živě aplikované a ověřené verifierem, dočasný smoke signál přes `db.set_title_role_signal()` byl uložen/načten/smazán a cílený pytest `tests/test_user_lists_postgres_overlay.py tests/test_postgresql_migration_offline.py` prošel `28 passed`.

## 2026-07-19 - UI pro role/postavy v Main cast menu

- Řádky v `Main cast` už nejsou celé jedním odkazem; odkaz na osobu zůstal na portrétu/jménu a vedle přibylo malé `...` menu.
- Menu nabízí `Otevřít herce` a serverový formulář `Role/postava` pro uložení titulově vázaného signálu.
- Formulář ukládá přes `POST /ui/title-role-signals/set` jméno postavy, více typů signálu přes checkboxy, polaritu, sílu 0-10 a poznámku; nepřidává novou vlastní JS logiku a používá existující menu mechanismus.
- Do typů signálu přibyly `visual_appeal` a `attraction`, aby šlo zachytit i vzhled/přitažlivost role v konkrétní době a kontextu.
- Přibylo `POST /ui/title-role-signals/delete` pro smazání hodnocení role.
- Poznámka se nepočítá číselně; je uložená jako vysvětlující kontext pro člověka a později AI.
- Popover menu se nově po otevření pozicuje podle skutečné výšky panelu, aby nepadalo mimo spodní okraj viewportu.
- Globální affinity k herci zůstává na detailu osoby, aby se nepletla osoba s konkrétní rolí v titulu.
- Ověření: rozšířený PostgreSQL constraint byl živě aplikovaný a ověřený, live smoke uložil/načetl/smazal více signálů včetně `visual_appeal` a `attraction`, `uv run pytest tests/test_ui_title_role_signals.py tests/test_user_lists_postgres_overlay.py tests/test_postgresql_migration_offline.py` prošel `30 passed` a render smoke detailu pro `tt0133093` vrátil `200`.

## 2026-07-19 - Role/postava signály v AI API kontraktu

- `GET /api/ai/context` nově vysvětluje `title_role_signal_strength`, povolené typy signálu, polarity a význam poznámky.
- Typy signálu v kontraktu jsou `character`, `dialogue`, `behavior`, `relationship_dynamic`, `performance`, `visual_appeal`, `attraction` a `other`.
- `GET /api/ai/taste-seed` vrací u každé položky nové pole `title_role_signals`.
- Každý role signal v payloadu obsahuje `signal_key`, `nconst`, `person_name`, `character_name`, `signal_type`, `polarity`, `strength`, `notes`, `source_origin`, `source_ref` a `updated_at`.
- `API_ENDPOINTY.md` byl aktualizovaný jako živý kontrakt pro navazující projekty.
- Ověření: cílený pytest `tests/test_api_ai_context.py tests/test_user_lists_postgres_overlay.py tests/test_ui_title_role_signals.py` prošel `15 passed`, `compileall` prošel a live smoke s dočasným role signálem potvrdil, že `taste-seed` vrací signály `attraction` a `dialogue`.

## 2026-07-19 - AI endpointy podle ratingu a ai_input_role

- Přibyl `GET /api/ai/rated-titles` pro tituly s lokálním hodnocením od zadaného prahu.
- Endpoint má query parametry `min_user_rating`, `limit` a volitelné `title_type`.
- Payload položek je kompatibilní s `/api/ai/taste-seed`: obsahuje lokální rating, slovní poznámky, people affinity, title role signals a genre score signály.
- Přibyl `GET /api/ai/taste-inputs`, který řeší výběr vstupů podle `app.user_lists.ai_input_role`.
- `taste-inputs` zahrnuje role `strong_positive`, `interested_owned`, `interested_planned`, `in_progress` a `negative`; `external_suggestion` a `ignore` vrací jen v `excluded_sources`, ne jako vstupní položky.
- Tím je výslovně zajištěno, že seznam `AI návrhy` nepůjde zpět jako vstup pro další AI doporučování.
- `API_ENDPOINTY.md` byl aktualizovaný pro oba endpointy.
- Ověření: `uv run pytest tests/test_api_ai_context.py` prošel `4 passed`, `compileall` prošel, live helper `get_ai_rated_titles(min_user_rating=8, limit=3)` vrátil položky s očekávanými AI poli a `get_ai_taste_inputs(limit_per_list=2)` potvrdil skupiny podle rolí i vyloučené AI návrhy.

## 2026-07-19 - Scoring explainer pro AI projekt

- Přibyl read-only endpoint `GET /api/ai/scoring-explainer`.
- Endpoint vysvětluje současný lokální scoring, hlavní principy a význam polí `final_score`, `watch_signal_score`, `rating_signal_score`, `actor_affinity_score`, `genre_score_signals`, `favorite_genres`, `favorite_traits`, `people_affinity` a `title_role_signals`.
- Důležité upřesnění: `title_role_signals` jsou zatím samostatná nová vrstva a nejsou započítané do `final_score` ani `genre_score_signals`.
- Vzdálený úkol je později navrhnout samostatnou scoring větev pro role/postava signály, například `role_signal_score` nebo `character_preference_signals`, bez mechanického zvedání celkového ratingu titulu.
- `API_ENDPOINTY.md` byl aktualizovaný ze stavu plánováno na implementováno.

## 2026-07-19 - Noted titles endpoint pro AI projekt

- Přibyl read-only endpoint `GET /api/ai/noted-titles`.
- Endpoint vrací tituly s neprázdnými `liked_notes` nebo `disliked_notes`; filtr `notes=any|liked|disliked` dovoluje vytáhnout všechny poznámky, jen klady nebo jen zápory.
- Volitelný `min_user_rating` umožní navazujícímu AI projektu kombinovat slovní poznámky s číselným prahem, ale endpoint není vázaný na seznamy.
- Payload položek drží stejný tvar jako `taste-seed`: IMDb/TMDB identita, lokální rating, poznámky, people affinity, title role signals a genre score signals.
- Ověření: `uv run pytest tests/test_api_ai_context.py` prošel `6 passed`, `compileall` prošel a live helper `get_ai_noted_titles(limit=3)` vrátil reálné položky s očekávanými klíči.

## 2026-07-19 - První import AI doporučení do AI návrhů

- V PostgreSQL přibyly auditní tabulky `app.ai_recommendation_runs` a `app.ai_recommendation_candidates`.
- Importní skript `python -m filmy.scripts.import_ai_recommendations <json>` čte stabilní výstup z `filmy_output`, validuje povinná pole a ukládá celý běh i jednotlivé kandidáty.
- Kandidát s IMDb ID se resolverem páruje na `app.catalog_titles.tconst`; resolved kandidát se upsertuje do seznamu `AI návrhy`.
- Deduplikace je záměrně jen v cílovém seznamu `AI návrhy` přes unikátní `(list_id, canonical_key)`. Nevadí tedy, když stejný titul už existuje ve Watchlistu, `Mam`, Plex Library nebo jiném seznamu.
- Importované byly tři aktuální výstupy z `filmy_output`: `watch-next`, `local-score-test` a `external-discovery-general`.
- Výsledek v DB: 3 importní běhy, 21 kandidátů, 18 unikátních IMDb ID a 18 aktivních položek v `AI návrhy`.
- Ověření: schema apply + fingerprint + role check prošly, `uv run pytest tests/test_ai_recommendations_import.py tests/test_postgresql_migration_offline.py tests/test_user_lists_postgres_overlay.py` prošel `31 passed`, `compileall` prošel.

## 2026-07-19 - filmy_output je stabilní zdroj importu

- Jiří určil, že `filmy_output/` se má v této fázi považovat za stabilní zdroj pro import AI doporučení zpět do FILMY.
- Standardní výstup je tedy JSON schéma popsané v `filmy_output/README.md`: všechna známá pole jsou přítomná vždy a nepoužitá pole mají hodnotu `null` nebo prázdný seznam.
- Z toho plyne, že importer ve FILMY nemusí v této fázi hádat volný textový tvar z AI chatu; má číst stabilní výstupní soubory z `filmy_output/`.
- Stále platí, že deduplikace má být jen uvnitř `AI návrhy`, ne proti jiným seznamům.

## 2026-07-19 - UI pro Import AI suggestions

- V horním menu `System` přibyla položka `Import AI suggestions`.
- Slepá položka `Import Tools` byla odstraněná.
- Nová stránka `/system/import-ai-suggestions` vypisuje JSON soubory z `filmy_output/`, počet doporučení, intent a stav, jestli už byl daný soubor importovaný.
- Import se spouští serverovým POSTem nad vybraným souborem; formulář neposílá libovolnou cestu, ale jen název souboru, který se ověří proti známým validním souborům z `filmy_output/`.
- Přibyla ochrana proti opakovanému importu stejného souboru přes unikátní `source_checksum` v `app.ai_recommendation_runs`; opakované spuštění vrátí `already_imported` a nezaloží nový běh.
- Ověření: schema apply + fingerprint + role check prošly, opakovaný import stejného `watch-next` JSONu vrátil `already_imported=True`, `uv run pytest tests/test_ui_import_ai_suggestions.py tests/test_ai_recommendations_import.py tests/test_postgresql_migration_offline.py` prošel `22 passed`, `compileall` prošel.

## 2026-07-19 - Mazání souborů z filmy_output v UI

- V sekci `Available files` na stránce `/system/import-ai-suggestions` přibylo u každého JSON souboru tlačítko `Smazat`.
- Mazání jde přes serverový POST `/system/import-ai-suggestions/delete`.
- Backend přijímá jen název souboru, ne libovolnou cestu, a aplikační helper povolí smazat pouze `.json` soubor přímo z adresáře `filmy_output/`.
- Ověření: cílené testy `tests/test_ui_import_ai_suggestions.py tests/test_ai_recommendations_import.py` prošly `8 passed`, `compileall` prošel a reálné aktuální soubory v `filmy_output/` zůstaly po testech na místě.

## 2026-07-19 - AI fit/risk důvody v detailu titulu

- Detail titulu nově načítá poslední importované AI doporučení podle `resolved_tconst`.
- `fit_reasons` z importovaného JSONu se zobrazují v bloku `Slovní hodnocení` pod sloupcem `Klady`.
- `risk_reasons` se zobrazují ve stejném bloku pod sloupcem `Zápory`.
- Obě části jsou označené jako `AI doporučení`, aby se nemíchaly s Jiřího vlastním slovním hodnocením.
- Pod blokem se zobrazuje zdrojový JSON soubor, confidence a čas importu.
- Ověření: cílený render test prošel, `uv run pytest tests/test_title_detail_ai_recommendation.py tests/test_ui_import_ai_suggestions.py tests/test_ai_recommendations_import.py` prošel `9 passed`, `compileall` prošel a live render `/titles/tt2316411` vrátil `200` s očekávaným AI fit i risk textem.

## 2026-07-19 - Odstranění starého databázového backendu z kódu

- Aplikační kód je PostgreSQL-only: odstraněná je `duckdb` dependency, backend přepínače v konfiguraci, staré connection/bootstrap moduly a jednorázový migrační skript ze souborové databáze.
- `filmy/db.py`, `filmy/db_library.py`, `filmy/db_people.py` a `filmy/runtime_postgres.py` už nemají runtime větve na starý souborový backend.
- `/api` a `get_catalog_stats()` už nevrací cestu k lokálnímu databázovému souboru, ale PostgreSQL databázi.
- Testy starého migračního můstku byly odstraněné nebo přepsané na PostgreSQL-only očekávání.
- Ověření: `uv run python -m compileall filmy tests` prošel a `uv run pytest` prošel `59 passed`.

## 2026-07-20 - AI návrhy nahradily Continue Watching na domovce

- Horní rail domovky už nečte `Continue Watching`, ale prvních několik položek ze seznamu `AI návrhy` (`ai-suggestions`).
- Karty používají běžný list read model, zobrazují badge `AI tip` a odkazují na detail s návratem do sekce `AI návrhy`.
- Poznámky a zdůvodnění z importu se v horním railu nezobrazují, protože na domovce působí matoucě; zůstávají pro detail nebo list view.
- `Show all` vede na `/lists/ai-suggestions`; prázdný stav posílá Jiřího na importní workflow `System -> Import AI suggestions`.
- Ověření: `uv run python -m compileall filmy tests` prošel a celý `uv run pytest` skončil `64 passed`.

## 2026-07-20 - Watched blacklist endpoint pro filmy-knihy

- Přibyl read-only endpoint `GET /api/ai/watched-titles` pro kompletní seznam titulů, které externí AI vrstva nemá znovu doporučovat.
- Endpoint není limitovaný seed; skládá unikátní `display_tconst` z `watch_events`, `content_state=watched`, `user_ratings` a volitelně negativních seznamů (`ai_input_role=negative`).
- Epizodní signály se normalizují na rodičovský seriál, aby `filmy-knihy` nefiltrovalo jen jednu epizodu a znovu nenavrhlo celý seriál.
- `API_ENDPOINTY.md` byl aktualizovaný jako kontrakt pro navazující projekt.
- Ověření: cílený API test prošel, `compileall` prošel a živý helper smoke v aktuální DB vrátil `item_count=1952`.

## 2026-07-20 - Vyčištění položek v AI návrzích

- Na Jiřího žádost byly odstraněné aktivní položky ze seznamu `AI návrhy`.
- Po domluvě bylo řešení změněné z archivace na fyzické mazání řádků v `app.user_list_items`, protože `AI návrhy` jsou pracovní inbox pro opakované AI doporučovací vlny a archivované položky by zbytečně rostly.
- Samotný seznam ani auditní tabulky importu se nemažou; `app.ai_recommendation_runs` a `app.ai_recommendation_candidates` zůstávají jako evidence importů a zdroj AI důvodů na detailu titulu.
- Výsledek v aktuální DB: po původní archivaci bylo fyzicky smazáno `18` řádků pro `ai-suggestions`; `get_user_list_items_page("ai-suggestions")` vrací aktivní počet `0`.
- Seznam `ai-suggestions` zůstal zachovaný se slugem `ai-navrhy` a rolí `external_suggestion`, takže další import AI doporučení ho může znovu naplnit.
- V UI přibylo oranžové tlačítko `Vyčistit` za `Edit`, zobrazuje se jen u seznamu `AI návrhy`.

## 2026-07-21 - Navázání a ověření AI doporučovací větve

- Po pauze byl zkontrolovaný rozdělaný pracovní strom kolem `AI návrhy`, endpointu `/api/ai/watched-titles` a čištění AI inboxu.
- Cílené testy pro AI homepage rail, AI API kontrakt a listové operace prošly `24 passed`; celý `uv run pytest` prošel `70 passed`.
- Živý helper smoke `get_ai_watched_titles()` nad aktuální PostgreSQL DB vrátil `item_count=1953` a source counts `watch_event=1918`, `content_state_watched=148`, `user_rating=276`, `negative_list=6`.
- `black` byl ponechaný jako vývojový nástroj, ale přesunutý z runtime dependencies do `dependency-groups.dev`.
- Jiří následně v reálném UI potvrdil, že `AI návrhy` fungují v pořádku včetně smazání položek.

## 2026-07-21 - Oprava vyhledávání Shelter

- Dotaz `Shelter` v titulovém hledání nevracel správný titul `tt0942384`, přestože byl v PostgreSQL katalogu a měl lokální watched/rating data.
- Příčina byla kombinovaná: `app.search_recall` měl pro `Shelter` uložený shortcut na `tt32357218`, recall vracel jen singleton kandidáta, základní katalogové řazení nepreferovalo exact/prefix shodu před obecnými substringy a `_pick_best_title_match()` řešil fuzzy skóre před přesnou-title disambiguation.
- Oprava: katalogový search řadí exact/prefix shody před substringy; u více přesných stejných názvů se recall shortcut nepoužije; exact-title disambiguation bere v úvahu lokální signály (`watched_count`, rating, watchlist/list membership).
- Ověření: `lookup_title_by_query("Shelter", title_type="movie")` vybírá `tt0942384`; route smoke `/search?q=Shelter&search_scope=titles&title_type=movie` vrátil `200` a obsahuje `tt0942384`; celý `uv run pytest` prošel `71 passed`.

## 2026-07-21 - Poznámka k pravidlům mezi seznamy

- Jiří upozornil, že vztahy mezi seznamy a akcemi je potřeba promyslet obecněji. Příklad: titul v `Koukni rychle` po označení jako viděný v seznamu zůstává.
- Nemá se to řešit jen hardcodem pro současné konkrétní seznamy. Smysluplnější kandidát je ručně editovatelný TOML soubor s pravidly, která sjednotí následky akcí typu `watched` vůči rolím nebo konkrétním seznamům.

## 2026-07-21 - Serverové mazání skupiny ze seznamu

- Bez zavádění nových pravidel vztahů mezi seznamy byl přesunutý malý write workflow `delete_group_from_user_list()` do PostgreSQL.
- Přibyla DB funkce `app.archive_user_list_group(...)`, která setově archivuje aktivní položky jedné zobrazené skupiny podle `display_tconst`; u epizod používá seriálového rodiče stejně jako dosavadní Python logika.
- Aplikační fasáda zachovala stejné chyby a návratový tvar, ale místo načítání aktivních položek a Python smyčky volá jeden PostgreSQL entrypoint.
- Lokální migrace `002/003` byly znovu aplikované do PostgreSQL. Rollback smoke nad dočasným seznamem ověřil `list_found=True`, `archived_items=1` a archivovaný řádek.
- Ověření: cílené testy prošly `26 passed`, `compileall` prošel a celý `uv run pytest` skončil `73 passed`.
