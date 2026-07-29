# Historie projektu

## 2026-07-29 - Prvni zapojeni editace List Action Rules

- Read-only detail [templates/system_list_action_rules_detail.html](templates/system_list_action_rules_detail.html) se zmenil na skutecny editor po jednom listu. U kazdeho triggeru `Set Rating`, `Mark Watched`, `Copy To List` a `Move To List` jsou ted editovatelne radky s `effect`, volitelnym `target list`, `phase`, `order`, stavem `active/disabled` a tlacitky pro ulozeni nebo smazani.
- Backend editoru je zamerne porad konzervativni a bez nove migrace. Do [filmy/runtime_postgres_title_sessions.py](filmy/runtime_postgres_title_sessions.py) pribyly write helpery `fetch_rule`, `upsert_rule` a `delete_rule`, facade wrappery jsou v [filmy/runtime_postgres.py](filmy/runtime_postgres.py) a formularove routy + validace v [filmy/routers/web_system.py](filmy/routers/web_system.py).
- Validace drzi dnesni odsouhlasene mantinely: `copy_to_list` a `move_to_list` musi mit konkretni cil, bezcilove akce cil mit nesmi, editor neumozni jako cil `watchlist` ani `ai-navrhy` a blokuje zjevne nesmyslnou kombinaci `move_to_list + preserve_source_membership`.
- Soucasne zustava zachovany princip, ze disabled radek je stale editovatelny, zatimco skutecne locknuta kombinace se v detailu jen zobrazi s duvodem a bez editace.
- Overeni proslo pres `python3 -m py_compile filmy/routers/web_system.py filmy/runtime_postgres.py filmy/runtime_postgres_title_sessions.py` a cileny pytest `tests/test_ui_system_list_action_rules.py`, ktery skoncil `5 passed`.
- Na navazujicim realnem UI smoke v browseru se ukazala jeste jedna render regrese: existujici pravidla se zobrazovala jako `Disabled`, prestoze helper data mela `enabled=True`. Pricina byla v Jinja vyhodnoceni status selectu; detail sablona ted pouziva explicitni pristup `rule["is_enabled"]` misto problematickeho vyhodnoceni pres teckovou notaci.
- Oprava byla overena dvojmo: helper vrstva nad `watchlist` vratila stale `enabled=True`, pytest znovu proslel a cista docasna lokalni instance aplikace v browser smoke uz vykreslila u existujicich pravidel `Active`. Zbyvajici console chyba byla jen nepodstatna `404 /favicon.ico`.
- Na tenhle posledni zbytek navazuje i nova favicona: v projektu pribyl asset [static/favicon.svg](static/favicon.svg), layout [templates/base.html](templates/base.html) ho zapojuje pres `rel="icon"` a [filmy/main.py](filmy/main.py) nove obsluhuje `/static` a presmerovani `/favicon.ico -> /static/favicon.svg`. Tj. browser smoke uz nema ani tenhle zbytecny 404 sum.

## 2026-07-29 - Prvni read-only UI pro List Action Rules

- V `System` menu pribyla nova polozka `List Action Rules` a prvni dve read-only stranky nad realnymi daty: overview [templates/system_list_action_rules_overview.html](templates/system_list_action_rules_overview.html) a detail jednoho listu [templates/system_list_action_rules_detail.html](templates/system_list_action_rules_detail.html).
- Overview zamerne neni kartovy dashboard, ale prostornejsi seznam po jednom radku na list. Ukazuje konkretni seznam, kind, AI roli, pocet pravidel, pocet locku a posledni rule update. Detail pak sklada pravidla po trigger akcich `Set Rating`, `Mark Watched`, `Copy To List` a `Move To List`.
- Backend pro tenhle UI rez zustal zamerne jednoduchy a read-only: [filmy/routers/web_system.py](filmy/routers/web_system.py) si bere listy a pravidla primo z PostgreSQL helperu a zadnou editaci nebo novy DB kontrakt zatim nepridava.
- Overeni proslo pres `python3 -m py_compile filmy/routers/web_system.py` a cileny pytest `tests/test_ui_system_list_action_rules.py tests/test_ui_import_ai_suggestions.py tests/test_home_ai_suggestions.py`, ktery skoncil `11 passed`.

## 2026-07-29 - Rozdeleni title-session domeny do samostatneho Python modulu

- Pred navazanim na UI editor pravidel probehl udrzovaci rez v runtime vrstve: title-session storage a orchestrace uz nelezi primo v [filmy/runtime_postgres.py](filmy/runtime_postgres.py), ale v novem modulu [filmy/runtime_postgres_title_sessions.py](filmy/runtime_postgres_title_sessions.py).
- Nova modulova hranice je zamerne konzervativni: verejne wrappery `fetch_list_action_rules(...)`, `upsert_title_session(...)`, `insert_title_session_action(...)`, `queue_title_session_action_effects(...)`, `apply_title_session_effects(...)` a `finalize_title_session(...)` zustaly v [filmy/runtime_postgres.py](filmy/runtime_postgres.py), takze `db_library.py` ani testy nemusely menit svuj kontrakt.
- Soucasne probehl maly cleanup fallback vetve v [filmy/db_library.py](filmy/db_library.py): opakovany write loop pro group `copy/move` je vytazeny do sdilenych helperu misto duplikace dvou skoro stejnych smycek.
- Overeni proslo pres `python3 -m py_compile filmy/runtime_postgres.py filmy/runtime_postgres_title_sessions.py filmy/db_library.py` a cileny pytest `tests/test_runtime_postgres_content_state.py tests/test_user_lists_postgres_overlay.py tests/test_postgresql_runtime_schema.py`, ktery skoncil `44 passed`.

## 2026-07-29 - Dokonceni backend napojeni pro copy/move mezi seznamy

- Title-session backend je dodelany i pro cilove list akce `copy_to_list` a `move_to_list`. V [filmy/db_library.py](filmy/db_library.py) se tyto write flow uz nejdriv snazi bezet pres novou session/orchestraci a jen pri chybejicich pravidlech spadnou zpet na puvodni prime PG upsert/archive smycky.
- Pro tenhle rez pribyl novy seed krok [009_list_action_target_rule_seed.sql](migrations/postgresql/009_list_action_target_rule_seed.sql). Zaklada prvni sadu pravidel pro dnesni realne seznamy a zamerne nepovoluje cile `watchlist` ani `ai-navrhy`, aby backend respektoval aktualne domluveny workflow.
- [filmy/runtime_postgres.py](filmy/runtime_postgres.py) nove umi u effectu `add_target_membership` obslouzit i `group_items`, takze jedna session akce muze pridat nebo presunout celou display skupinu polozek misto jednoho samotneho titulu.
- Testy pokryvaji jak session cestu, tak fallback cestu pro `move/copy`, plus novy schema/upgrade kontrakt a group-item orchestraci.
- Overeni proslo pres `python3 -m py_compile filmy/db_library.py filmy/runtime_postgres.py filmy/scripts/upgrade_database.py`, cileny pytest `tests/test_user_lists_postgres_overlay.py tests/test_runtime_postgres_content_state.py tests/test_postgresql_runtime_schema.py` se `44 passed` a `python -m filmy.scripts.upgrade_database --dry-run` uz vypisuje i krok `0009-list-action-target-rule-seed`.

## 2026-07-29 - Prvni backend napojeni title-session write flow

- Nad predchozi DB kostrou, helper vrstvou a finalize orchestraci uz pribylo prvni skutecne backendove napojeni do dnesnich write flow v [filmy/db_library.py](filmy/db_library.py).
- `set_user_rating(...)` a `record_watch_event(...)` se ted nejdriv pokusi bezet pres title-session workflow: umi odvodit `source_list_id` nejen z explicitniho parametru, ale i z `return_to` URL nebo vnoreneho `return_to`, a podle `app.list_action_rules` rozhodnou, jestli maji pouzit novou orchestraci.
- Prakticky to zatim drzi bezpecny kompromis: kdyz pro zdrojovy seznam nejsou seedovana pravidla nebo kontext nelze spolehlive odvodit, zapis neselze a spadne zpet na dosavadni prime PostgreSQL write chovani.
- V [filmy/routers/ui.py](filmy/routers/ui.py) se watched/rating formulare uz propisuji i s `return_to`; rating routy navic umi prijmout volitelne `source_list_id`, aby slo pozdeji bez dalsiho API lomu doplnit explicitni UI wiring.
- Overeni proslo pres `python3 -m py_compile filmy/db_library.py filmy/db.py filmy/routers/ui.py` a cileny pytest `tests/test_user_lists_postgres_overlay.py tests/test_postgresql_runtime_schema.py tests/test_runtime_postgres_content_state.py`, ktery skoncil `40 passed`.

## 2026-07-29 - Databazovy navrh pro rule builder a title session

- Na scenarovou matici a obecny rule-builder navazal konkretnejsi technicky navrh [LIST_ACTION_DB_SCHEMA_DRAFT.md](LIST_ACTION_DB_SCHEMA_DRAFT.md), ktery uz rozepisuje predpokladane PostgreSQL tabulky a beh session nad jednim titulem.
- Navrh rozdeluje problem do tri vrstev: konfiguracni `list_action_rules`, kratkodobe `title_sessions` + `title_session_actions` a operacni `title_session_effect_queue`, kam se ma zapisovat konkretni execution plan odvozeny z pravidel a z uzivatelskych kroku.
- Dulezite rozhodnuti v navrhu: `derive_watched` a dalsi immediate write efekty se maji provadet hned, ale session ma zustat otevrena, aby po ratingu nebo watched slo nad stejnym titulem jeste delat `copy_to_list` nebo `move_to_list`.
- Dokument zaroven zamerne drzi prvni implementaci pri zemi: pravidla se edituji po konkretnim listu, ne pres role sablony; `title_session*` tabulky jsou jen orchestracni vrstva nad existujicimi rating/watch/list tabulkami; a UI editor se ma resit az po overeni databazove kostry a prvniho finalize flow.

## 2026-07-29 - Prvni implementacni rez: DB kostra pro list actions a title session

- Z navrhove dokumentace uz vznikl prvni skutecny databazovy rez. Pribyly nove verziovane migrace [006_list_actions_session_schema.sql](migrations/postgresql/006_list_actions_session_schema.sql) a [007_list_actions_session_grants.sql](migrations/postgresql/007_list_actions_session_grants.sql).
- Schema krok `006` zavadi ctyri nove orchestration tabulky `app.list_action_rules`, `app.title_sessions`, `app.title_session_actions` a `app.title_session_effect_queue`, jejich check constrainty, indexy a `touch_updated_at` triggery pro upravovane tabulky.
- Prakticky zamer tohohle rezu je ziskat pevnou PostgreSQL kostru bez toho, aby se hned michala nova session logika do existujicich write endpointu. Proto zatim nepribyly zadne finalize funkce ani backend napojeni; jen pripraveny datovy zaklad.
- Upgrade runner [filmy/scripts/upgrade_database.py](filmy/scripts/upgrade_database.py) uz zna nove kroky `0006-list-actions-session-schema` a `0007-list-actions-session-grants`.
- Ověření: cílený `pytest` nad [tests/test_postgresql_runtime_schema.py](tests/test_postgresql_runtime_schema.py) skončil `7 passed` a `--dry-run` upgradu vypsal nové kroky `006` a `007` ve správném pořadí.

## 2026-07-29 - Druhy implementacni rez: runtime helper vrstva pro title session

- Nad novou databazovou kostrou uz pribyla prvni Python orchestration vrstva v [filmy/runtime_postgres.py](filmy/runtime_postgres.py). Nova trida `TitleSessionStore` drzi pohromade nacitani pravidel, zalozeni nebo obnoveni session, zapis explicitnich session akci a cteni nebo zapis effect queue.
- Zpetne kompatibilni wrappery v modulu jsou zatim ciste storage API, bez finalize rozhodovaci logiky: `fetch_list_action_rules(...)`, `upsert_title_session(...)`, `fetch_title_session(...)`, `insert_title_session_action(...)`, `fetch_title_session_actions(...)`, `insert_title_session_effect_rows(...)` a `fetch_title_session_effect_queue(...)`.
- Tohle je zamerne oddelene od stavajicich write endpointu. Cilem druheho rezu bylo mit nejdriv overeny runtime helper kontrakt, aby finalize engine nemusel vznikat naslepo primo v routerech nebo `db_library.py`.
- Ověření: cílený `pytest` nad [tests/test_postgresql_runtime_schema.py](tests/test_postgresql_runtime_schema.py) a [tests/test_runtime_postgres_content_state.py](tests/test_runtime_postgres_content_state.py) skončil `20 passed`.

## 2026-07-29 - Treti implementacni rez: prvni finalize orchestrator

- Nad storage vrstvou uz vznikla i prvni skutecna orchestrace v [filmy/runtime_postgres.py](filmy/runtime_postgres.py). Nova trida `TitleSessionOrchestrator` umi z jedne session akce a odpovidajicich `list_action_rules` sestavit effect queue, ulozit ji do `app.title_session_effect_queue` a spustit vybranou phase.
- Prvni funkcni wrappery jsou `queue_title_session_action_effects(...)`, `apply_title_session_effects(...)` a `finalize_title_session(...)`. Zatim je to porad bez napojeni do routeru nebo `db_library.py`; smyslem bylo nejdriv overit, ze samotna orchestracni vrstva funguje a ma testovatelny kontrakt.
- V prvnim rezu umi orchestrator bezpecne obslouzit ty effect typy, ktere uz maji prirozeny low-level backend helper: `write_rating`, `write_watched`, `add_target_membership` a `deactivate_source_membership`. Neutralni kroky typu `derive_watched`, `preserve_*` a `noop` se jen korektne propisou jako aplikovane queue radky.
- Ověření: cílený `pytest` nad [tests/test_runtime_postgres_content_state.py](tests/test_runtime_postgres_content_state.py) a [tests/test_postgresql_runtime_schema.py](tests/test_postgresql_runtime_schema.py) skončil `23 passed`.

## 2026-07-29 - Konkretni matice scenaru pro list actions

- K obecnému návrhu `title session` přibyl konkrétní mezikrok [LIST_ACTIONS_SCENARIO_MATRIX.md](LIST_ACTIONS_SCENARIO_MATRIX.md), který rozepsal dnešní skutečné FILMY seznamy proti běžným akcím `mark_watched`, `set_rating`, `move_to_list` a `copy_to_list`.
- Matice používá reálné dnešní seznamy z databáze (`Watchlist`, `Koukni rychle`, `Kouknout znou`, `Mam`, `Plex Library`, `Rozkoukáno`, `AI návrhy`, `Nedokoukáno`, `Stáhnout`) a u každého případu rozlišuje `immediate write`, `finalize effect` a `preserve`.
- Praktický cíl této matice je zúžit další technický návrh: ukazuje, že `set_rating` a `mark_watched` mohou být pravděpodobně immediate write, zatímco cleanup vztahů mezi seznamy patří až do `finalize_title_session(...)`.
- Tím se zpřesnil další očekávaný DB řez: místo slepého parseru pravidel nebo okamžitého runtime engine má následovat návrh `title_sessions`, `title_session_actions`, `pending_membership_changes` a serverového finalize kroku.

## 2026-07-29 - Posun od scenarove matice k obecnemu rule builderu

- Pri dalsim rozboru se ukazalo, ze samotna scenarova matice jeste neni dost obecna pro budouci editovatelne UI. Jiří upřesnil, že nechce sadu pevně předepsaných kombinací, ale jednotný editor pravidel, kde každý list uvidí stejnou sadu akcí a jen nesmyslné kombinace budou zamčené.
- Vznikl proto nový technický návrh [LIST_ACTION_RULE_BUILDER_DRAFT.md](LIST_ACTION_RULE_BUILDER_DRAFT.md). Zavádí pevné typy `trigger_action` (`set_rating`, `mark_watched`, `copy_to_list`, `move_to_list`, ...) a pevné typy `effect_type` (`write_rating`, `derive_watched`, `write_watched`, `add_target_membership`, `archive_source_membership`, `preserve_source_membership`, ...).
- Současně je v návrhu i pracovní tvar jednoho řádku pravidla a celé skupiny kroků pro jednu akci, takže další DB návrh se už nebude odvíjet od ručně rozepsaných vět, ale od kontrolovaného modelu, který půjde validovat v UI i backendu.

## 2026-07-29 - Technicke navazani po cleanupu a smoke pred commitem

- Navazani na posledni cleanup checkpoint probehlo bez dalsich oprav kodu: nejdriv proslo staticke overeni `python3 -m py_compile` nad hlavni FastAPI/router/DB vrstvou.
- Cileny testovaci rez `uv run pytest tests/test_home_ai_suggestions.py tests/test_user_lists_postgres_overlay.py` skoncil `19 passed`; jedina hlaska byl znamy `StarletteDeprecationWarning` kolem `fastapi.testclient`.
- Pokus o zvednuti dalsi lokalni instance pres `.venv/bin/python main.py` ukazal, ze port `127.0.0.1:8019` uz byl obsazeny, takze smoke pokracoval nad bezici lokalni instanci misto slepeho druheho startu.
- HTTP smoke nad `127.0.0.1:8019` vratil `200` pro `/` (`~0.79 s`), `/titles/tt0133093` (`~1.22 s`), `/titles/tt0133093/main-cast` (`~0.79 s`), `/lists/watchlist` (`~1.22 s`) a `/api/ai/context` (`~1.26 s`); v odpovedich byly pritomne ocekavane markery pro `Main cast`, watchlist flow a AI context payload.
- Prakticky dalsi krok se tim nemení: pred commitem porad chybi uz jen kratke realne pouziti appky primo Jirim a az potom ma navazat horky ukol vztahu mezi listy / `Watched`.

## 2026-07-28 - Dalsi audit dlouhych souboru, nova `ImportBatchStore` class a dalsi FastAPI response modely

- Repo-wide audit dlouhych Python souboru ukazal, ze dalsi nejvetsi kandidati po docstringovem cleanupu jsou hlavne `filmy/runtime_postgres.py`, `filmy/app_shared.py` a `filmy/routers/api.py`; naopak `db.py`, `db_lookup.py` a `db_legacy.py` jsou sice stale dlouhe, ale uz ted slouzi spis jako facade nebo tematicka seskupeni nez jako jeden nerozlisitelny monolit.
- V `filmy/runtime_postgres.py` pribyla skutecna Python trida `ImportBatchStore`, ktera soustredila importni preview/commit storage workflow (`create_import_batch_record`, `insert_import_rows`, `fetch_import_batch_record`, `fetch_import_batch_rows`, `fetch_resolved_import_rows`, `commit_import_batch`, `fetch_existing_import_commits`). Puvodni funkce zustaly zachovane jako tenke wrappery, aby se nerozbily existujici volaci body ani testovaci patch points.
- V `filmy/routers/api.py` pribyly dalsi explicitni `response_model` kontrakty i mimo puvodni AI endpointy: IMDb manifest, rebuild katalogu, content-state mutation, knihovni watchlist/rating/watch write endpointy a import preview/batch/commit workflow. FastAPI vrstva tak ma o neco pevnejsi OpenAPI a validacni hranice i v casti admin/write API.
- Overeni: `python3 -m py_compile filmy/runtime_postgres.py filmy/routers/api.py` proslo.

## 2026-07-28 - Repo-wide docstringovy cleanup dokoncen v celem `filmy/`

- Repo-wide audit nad `filmy/**/*.py` je po posledni davce na `0` chybejicich docstringu. Krome velkych DB facade modulu byly dotazene i modulove docstringy v routerech, integracich, AI/helper modulech a malych CLI skriptech.
- Posledni zbytky byly hlavne technicke: modulove docstringy kvuli poradi s `from __future__ import annotations`, helpery v `app_shared.py` a `runtime_postgres.py`, starsi `legacy/trakt_export.py` a rebuild skript `scripts/rebuild_catalog_postgresql.py`.
- Overeni: opakovane `python3 -m py_compile` nad dotcenymi soubory proslo a repo-wide AST kontrola nad `filmy/**/*.py` ukazala `TOTAL_FILES_WITH_MISSING=0`.

## 2026-07-28 - Docstringovy cleanup `db.py`

- `filmy/db.py` je po samostatnem rezu na `0` chybejicich docstringu. Dopsana je centralni facade, kompatibilni wrappery nad rozrezanymi moduly `db_lookup`, `db_presentation`, `db_library`, `db_ai`, `db_tmdb`, `db_legacy` i spodni shared helper vrstva pro import preview, fingerprinty, local identity, Trakt diff utility a dalsi male runtime helpery.
- V tomhle modulu jsem zamerne nepridaval dalsi novou Python class jen kvuli dokumentacnimu cleanupu. Vetsina zbytku uz byla tenka facade nebo nizkourovnove utility; dalsi class obalka by tady spis zvysila hluk nez citelnost.
- Overeni: `python3 -m py_compile filmy/db.py` proslo. Po tehle davce je hlavni DB facade vrstva docstringove srovnana a dalsi krok je zbyvajici repo-wide audit dlouhych souboru, dalsich smysluplnych `class` a explicitni FastAPI review.

## 2026-07-28 - Docstringovy cleanup `db_legacy.py`

- `filmy/db_legacy.py` je po samostatnem rezu na `0` chybejicich docstringu. Dopsane jsou verejne legacy facade pro Trakt exporty, IMDb CSV seznamy a Plex bootstrap sync, ale i interni `_sync_trakt_*`, `_sync_imdb_*`, `_sync_plex_*` bloky a kompatibilni wrapper `_PgCompatConnection`.
- V tomhle modulu jsem zamerne nepridaval dalsi novou Python class. Prirodzena class hranice uz existovala v `_PgCompatConnection` a dalsi service obalka by spis umelo prebalovala procedurani legacy logiku bez realneho zjednoduseni.
- Overeni: `python3 -m py_compile filmy/db_legacy.py` proslo. Po tehle davce je nejvetsim zbylym docstringovym dluhem hlavne centralni facade `filmy/db.py`.

## 2026-07-28 - Dalsi repo-wide cleanup: background skripty a LocalLibraryReadModelSupport

- Operacni a background vrstva uz nema slepa mista v dokumentaci: na 0 chybejicich docstringu byly dotažene moduly `filmy/background_jobs.py`, `filmy/genre_scoring.py`, `filmy/scripts/run_imdb_refresh.py`, `filmy/scripts/run_metadata_pipeline.py`, `filmy/scripts/run_tmdb_backfill.py`, `filmy/scripts/materialize_title_details.py`, `filmy/scripts/materialize_person_details.py` a `filmy/scripts/materialize_person_portraits.py`.
- V `filmy/db_library.py` pribyla realna Python trida `LocalLibraryReadModelSupport`, ktera soustredila kratkou in-memory cache pro library status a opakovanou logiku kolem listovych read modelu (`episode -> series`, watched display seskupeni, group cards a detail skupin). Puvodni funkce zustaly zachovane jako tenke wrappery, aby se nerozbil zbytek aplikace ani testovaci patch points.
- `filmy/db_library.py` soucasne dostalo podrobnejsi docstringy i na verejnou fasadu; po teto davce je docstringove kompletni. Nejvetsi zbytek cleanupu tak zustava hlavne v `filmy/db.py`, `filmy/db_lookup.py`, `filmy/db_tmdb.py`, `filmy/db_legacy.py` a `filmy/db_presentation.py`.

## 2026-07-28 - Docstringovy cleanup `db_lookup.py`

- `filmy/db_lookup.py` je po samostatnem rezu na `0` chybejicich docstringu. Dopsane jsou nejen verejne facade funkce `lookup_*` a `describe_*`, ale i engine metody, kandidatni prevody, recall helpery, confidence pravidla, SQL lookup helpery a similarity utility.
- V tomhle modulu uz predtim existovaly smysluplne realne Python tridy `TitleLookupEngine` a `PersonLookupEngine`, takze dalsi trida by byla umele vrstveni bez jasneho prinosu. Cleanup se proto omezil na dokumentaci a citelnost, ne na dalsi prebalovani logiky.
- Overeni: `python3 -m py_compile filmy/db_lookup.py` proslo. Po teto davce mezi nejvetsimi docstringovymi dluhy zustavaji hlavne `filmy/db.py`, `filmy/db_tmdb.py`, `filmy/db_legacy.py` a `filmy/db_presentation.py`.

## 2026-07-28 - Docstringovy cleanup `db_tmdb.py`

- `filmy/db_tmdb.py` je po samostatnem rezu na `0` chybejicich docstringu. Dopsane jsou verejne facade funkce kolem TMDB mapovani a assetu, completion helpery, target-selection helpery, cache targety i utility pro lokalni asset cesty a URL.
- Stejne jako u `db_lookup.py` jsem tam nepridaval dalsi umelou tridu navic. Modul uz mel vhodne hranice v existujicich datovych tridach `TmdbTargetItem`, `TitleDetailCacheTarget` a `PersonDetailCacheTarget`; dalsi cleanup byl proto ciste o citelnosti a dokumentaci.
- Overeni: `python3 -m py_compile filmy/db_tmdb.py` proslo. Po teto davce zustavaji jako nejvetsi docstringove dluhy hlavne `filmy/db.py`, `filmy/db_legacy.py` a `filmy/db_presentation.py`.

## 2026-07-28 - Docstringovy cleanup `db_presentation.py`

- `filmy/db_presentation.py` je po samostatnem rezu na `0` chybejicich docstringu. Dopsane jsou render helpery, cesty k disk cache, portrait/biography utility, cache-status helpery, load/store helpery, source-fingerprint logika i TMDB cache-signature utility.
- V tomhle modulu uz predtim byly spravne zvolene realne Python builder tridy `TitlePresentationBuilder` a `PersonPresentationBuilder`, takze dalsi trida by jen umela vrstvila logiku bez jasneho zisku. Cleanup byl proto zamerne dokumentacni.
- Overeni: `python3 -m py_compile filmy/db_presentation.py` proslo. Po teto davce zustava jako nejvetsi zbytek hlavne `filmy/db_legacy.py` a hlavni facade `filmy/db.py`.

## 2026-07-28 - Repo-wide docstring cleanup a nova TmdbClient class

- V `filmy/integrations/tmdb.py` pribyla skutecna servisni trida `TmdbClient`, ktera centralizuje TMDB rate-limit, konfiguracni cache, retry politiku, enrichment orchestrace a praci s lokalnimi metadata soubory. Verejne funkce zustaly zachovane jako tenke wrappery, aby se nerozbil zbytek aplikace ani testovaci patch points.
- Doplneny chybejici docstringy v dalsi sade modulu, ktere uz ted nevyzaduji dohledavani implicitni znalosti z chatu: `filmy/routers/api.py`, `filmy/routers/ui.py`, `filmy/db_ai.py`, `filmy/config.py`, `filmy/db_people.py`, `filmy/imdb_refresh.py`, `filmy/ai_recommendations.py`, `filmy/suggestion_engine.py`, `filmy/markdown.py` a `filmy/integrations/plex.py`.
- Prubezny audit po teto davce potvrdil, ze nejtezsi zbytek uz neni v routerech ani integracich, ale hlavne v rozsahlych DB facade modulech (`db.py`, `db_lookup.py`, `db_tmdb.py`, `db_legacy.py`, `db_library.py`, `db_presentation.py`) a v nekolika operacnich skriptech. Dalsi cleanup proto ma jit po techto blocich inkrementalne.

## 2026-07-28 - FastAPI cleanup: mensi web routery, response modely a breadcrumb class

- `filmy/routers/web.py` uz neni monoliticky router. Zustal jako skladaci vstupni bod a konkretni HTML routy se rozdelily do mensich modulu `web_home.py`, `web_search.py`, `web_lists.py`, `web_suggestions.py`, `web_system.py` a `web_titles.py`.
- Kvuli bezpecnemu refaktoru a zachovani starych testu zustal v `filmy.routers.web` zpetne kompatibilni patchovaci povrch pro monkeypatch a route smoke testy; nove moduly jej vedome pouzivaji jako compatibility vrstvu.
- `filmy/routers/api.py` dostal prvni skutecne `response_model` kontrakty pro root API, katalogove wrappery a hlavni AI endpointy (`/api/ai/context`, `taste-seed`, `taste-inputs`, `rated-titles`, `noted-titles`, `watched-titles`, `scoring-explainer`), aby FastAPI vrstva mela lepsi validaci a OpenAPI popis bez slepeho modelovani vsech internich admin payloadu.
- V `filmy/app_shared.py` pribyla realna Python trida `BreadcrumbNavigation`, ktera soustredila vicekrokovou breadcrumb/return-to logiku. Puvodni helper funkce zustaly jako tenke wrappery kvuli kompatibilite, ale logika uz ma jedno centralni misto a podrobne docstringy.
- Zaroven byly dopsane dalsi docstringy v novych router modulech, API response modelech a v navigacni vrstve, aby refaktor nezanechal dalsi bezejmenne helpery.
- Overeni: `python3 -m compileall filmy/app_shared.py filmy/routers filmy/routers/api.py` proslo. Cileny pytest `tests/test_home_ai_suggestions.py tests/test_title_detail_ai_recommendation.py tests/test_search_lookup.py tests/test_ui_import_ai_suggestions.py tests/test_api_ai_context.py` skoncil `19 passed`.

## 2026-07-28 - Pokracovani rozrezani `filmy/db.py` o TMDB a presentation bloky

- V ramci uklidu `filmy/db.py` byly dopsane dalsi tematicke rezy: TMDB vrstva je nove soustredena v `filmy/db_tmdb.py`, zatimco drive rozdelene AI, lookup a presentation/cache bloky uz bezne ziji v `filmy/db_ai.py`, `filmy/db_lookup.py` a `filmy/db_presentation.py`.
- `filmy.db` zustava zamerne jen jako stabilni fasada: verejne funkce i vybrane interni helpery dal existuji jako wrappery, aby se nemusel hromadne menit zbytek aplikace a aby testy mohly porad patchovat symboly pres `filmy.db`.
- Behem TMDB rezu se ukazalo, ze novy modul nesmi obchazet patchovaci povrch a sahat natvrdo na vlastni importy z `runtime_postgres`; finalni verze proto vraci cteni i zapis TMDB helperu zpet pres symboly v `filmy.db`, i kdyz implementace zije v `db_tmdb.py`.
- Zaroven byl srovnany navratovy tvar `get_person_detail_cache_targets()`, aby zustal kompatibilni se skripty materializace person detail cache.
- Overeni: `python3 -m compileall filmy/db.py filmy/db_tmdb.py filmy/db_people.py` proslo a cileny pytest `tests/test_tmdb_target_selection.py tests/test_tmdb_postgres_overlay.py tests/test_title_detail_ai_recommendation.py` skoncil `8 passed`.

## 2026-07-28 - Builder tridy v presentation vrstve a pravidlo pro pouzivani `class`

- V `filmy/db_presentation.py` pribyly skutecne Python builder tridy `TitlePresentationBuilder` a `PersonPresentationBuilder`, protoze skladani title/person presentation uz bylo vicekrokove a drzelo sdileny kontext, ktery prestaval byt citelny jako dlouhe volne funkce.
- `get_title_presentation()`, `get_title_people_panel()`, `_fetch_person_cache_source_detail()` a `_get_title_presentation_cached()` zustaly kompatibilni navenek, ale vnitrne uz pouzivaji builder tridy misto dalsiho rustu proceduralnich helperu.
- Soucasne bylo do `AGENTS.md` zapsane trvale pravidlo: kdyz v Pythonu dava smysl skutecna `class` jako nosic vicekrokove logiky nebo sdileneho kontextu, ma se pouzit i bez explicitniho vyzadani od Jiriho; zaroven pro takove tridy a jejich dulezite metody maji byt psane podrobne docstringy cesky bez hacku a carek.
- Overeni: `python3 -m compileall filmy/db_presentation.py filmy/db_lookup.py filmy/db.py filmy/db_people.py` proslo a cileny pytest `tests/test_title_detail_ai_recommendation.py tests/test_search_lookup.py tests/test_tmdb_postgres_overlay.py` skoncil `7 passed`.

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

-## 2026-07-28 - Pokracovani rozrezani `filmy/db.py` o TMDB a presentation bloky
-
- V ramci uklidu `filmy/db.py` byly dopsane dalsi tematiche rezy: TMDB vrstva je nove soustredena v `filmy/db_tmdb.py`, zatimco drive rozdelene AI, lookup a presentation/cache bloky uz bezne ziji v `filmy/db_ai.py`, `filmy/db_lookup.py` a `filmy/db_presentation.py`.
- `filmy.db` zustava zamerne jen jako stabilni fasada: verejne funkce i vybrane interni helpery dal existuji jako wrappery, aby se nemusel hromadne menit zbytek aplikace a aby testy mohly porad patchovat symboly pres `filmy.db`.
- Behem TMDB rezu se ukazalo, ze novy modul nesmi obchazet patchovaci povrch a sahat natvrdo na vlastni importy z `runtime_postgres`; finalni verze proto vraci cteni i zapis TMDB helperu zpet pres symboly v `filmy.db`, i kdyz implementace zije v `db_tmdb.py`.
- Zaroven byl srovnany navratovy tvar `get_person_detail_cache_targets()`, aby zustal kompatibilni se skripty materializace person detail cache.
- Overeni: `python3 -m compileall filmy/db.py filmy/db_tmdb.py filmy/db_people.py` proslo a cileny pytest `tests/test_tmdb_target_selection.py tests/test_tmdb_postgres_overlay.py tests/test_title_detail_ai_recommendation.py` skoncil `8 passed`.
-
## 2026-07-23 - Návrh title session a dokumentační základ budoucího manualu

- Na základě nového rozboru vztahů mezi seznamy vznikl pracovní návrh, že akce nad titulem nemají vždy okamžitě spouštět všechny doménové důsledky.
- Důvod je vícekrokový workflow: Jiří může při práci s jedním titulem zadat rating, odskočit na detail herce a po návratu ještě dělat `Move to` nebo `Copy to`.
- Vznikl technický návrh [LIST_ACTIONS_AND_TITLE_SESSION.md](LIST_ACTIONS_AND_TITLE_SESSION.md), který zavádí pracovní pojmy `title session`, `pending actions` a `finalize`.
- Současně vznikl lidsky psaný výklad [MANUAL_TITLE_WORKFLOW_DRAFT.md](MANUAL_TITLE_WORKFLOW_DRAFT.md), který je určený hlavně pro Jiřího a může být později základem napovědy nebo celého manuálu programu.

## 2026-07-22 - Oprava deploy cesty pro Mac mini LaunchAgent

- Šablona `deploy/cz.kulda.filmy.plist` byla opravena z chybné cesty `/Volumes/kulda/apps/FILMY` na skutečnou cestu `/Users/kulda/apps/FILMY`.
- Stejná oprava byla propsaná i do `README.md` a `INSTALACE.md`, aby nasazení na `mac-mini` odpovídalo reálnému umístění projektu.
- Dvojité lomítko z ručně dopsaného návrhu bylo při zápisu jen normalizované; význam cesty zůstal stejný.

## 2026-07-23 - Oprava Tailscale/Caddy HTTPS vrstvy na Mac mini

- Původní App Store build `Tailscale.app` na `mac-mini` byl rozbitý pro CLI i `.ts.net` HTTPS vrstvu: `tailscale status` padal na `BundleIdentifiers.swift:47`, chyběl `/var/run/tailscaled.socket` a `Caddy` přes `.ts.net` vracel TLS internal error.
- Po přechodu na standalone Tailscale build se CLI rozběhlo přes `TAILSCALE_BE_CLI=1`, aktivní network extension byla potvrzena a skutečný blocker se zúžil na změněnou identitu node po reinstalaci.
- Nový aktivní node byl nejdřív `kulda-mini-3` s IP `100.91.68.48`, zatímco starý `mini.taildce711.ts.net` dál mířil na offline node `100.124.124.95`. Po přejmenování v Tailscale adminu se finální MagicDNS hostname ustálil na `kulda-mini.taildce711.ts.net`.
- `Caddyfile` byl přepnutý na nový hostname/IP a HTTPS bylo ověřené end-to-end: `curl -vk https://kulda-mini.taildce711.ts.net:8019` vrátil `HTTP/2 200`; `curl -skI` na `8019` i `8020` vrací očekávané `HTTP/2 405` s `allow: GET`, `server: uvicorn` a `via: 1.1 Caddy`.
- Pro `Caddy` přibyl samostatný `launchd` plist `deploy/cz.kulda.caddy.plist`, určený pro `LaunchDaemon` v `/Library/LaunchDaemons`. Tím proxy vrstva běží i bez otevřeného Terminálu.
- Při stabilizaci se ukázalo, že vedle systémového `LaunchDaemon`u zůstával starý uživatelský `~/Library/LaunchAgents/cz.kulda.caddy.plist`, který dělal duplicitní starty a chyby `127.0.0.1:2019 already in use`. Po jeho odstranění a vyprázdnění logu už nové chyby nepřibývají.
- Ověřený provozní stav na `mac-mini` je:
  - `FILMY`: `https://kulda-mini.taildce711.ts.net:8019`
  - druhá appka: `https://kulda-mini.taildce711.ts.net:8020`
- Otevřená poznámka: z tohoto Macu zatím Jiří nepotvrdil reálné načtení těch URL v prohlížeči; zatím je potvrzený shell/curl průchod na samotném `mac-mini`. Pokud mají být odkazy považované za hotově dosažitelné i z jiného zařízení, je potřeba ještě samostatný klientský smoke mimo `mac-mini`.

## 2026-07-23 - Dokonceni serveroveho deploye FILMY na Mac mini

- Pri prvnim realnem pouziti noveho serveroveho upgrade runneru se ukazaly dve kompatibilitni opravy: PostgreSQL bootstrap/check musi tolerovat `pg_database_owner` jako legitimniho vlastnika schematu `public` a bezny serverovy upgrade nad existujici DB nema znovu prehravat `001_bootstrap.sql`, kdyz uz schema `app` existuje.
- Obe opravy byly dopsane jako follow-up commity (`f1a33c9`, `65d811d`) a po `git pull` na `mac-mini` probehl databazovy upgrade uspesne az do `Database upgrade OK.`.
- Prakticky overena serverova varianta upgradu na tomto stroji je `.venv/bin/python -m filmy.scripts.upgrade_database`; zkratka `uv run filmy-upgrade-database` tu zatim neni spolehliva, protoze `uv sync` neinstaluje `project.scripts` entrypointy.
- Soucasne byl opraven startup guard pro PostgreSQL katalog bez lokalnich `imdb/*.tsv` a TMDB asset read vrstva byla udelana prenositelna mezi stroji i pri starych absolutnich `local_path`.
- Po restartu `cz.kulda.filmy` se na `mac-mini` vse zobrazuje normalne a problematicky badge `TMDB fetching in background` zmizel.

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

## 2026-07-21 - Pravidlo pro serverové DB upgrady

- Po přenosu projektu na server se už nemá databáze ručně přenášet mezi stroji při dalších změnách.
- Každá další změna PostgreSQL schématu, funkce, view, constraintu, indexu nebo seed/role dat musí mít idempotentní upgrade krok v repozitáři.
- Přibyl runner `filmy.scripts.upgrade_database` / `filmy-upgrade-database`, který zakládá ledger `app.database_upgrades` a spouští verziované migrace.
- Instalační postup byl doplněný tak, aby se po `git pull` a `uv sync` spouštělo `uv run filmy-upgrade-database`.
