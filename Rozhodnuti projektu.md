# Rozhodnuti projektu

## 2026-07-13 - DuckDB zůstává pouze jako izolovaná údržbová a rollback vrstva

Rozhodnutí: neodstraňovat zatím možnost vědomého návratu k DuckDB ani jeho katalogový rebuild, ale vlastnictví přímých DuckDB spojení držet mimo běžný runtime v `filmy/db_bootstrap.py`.

Důvod: aktivní konfigurace a uživatelské cesty jsou PostgreSQL-first, přesto je během dokončování migrace užitečné zachovat kontrolovaný rollback. Úplné smazání fallbacku by bylo zbytečně destruktivní; ponechání přímých spojení roztroušených v `filmy/db.py` by naopak zamlžovalo hranici runtime a údržby.

Důsledek: `filmy/db.py` neobsahuje žádný explicitní `duckdb.connect()` ani `open_duckdb_connection()`. Legacy čtecí větve používají centrální retry wrapper a přímé otevření databáze je soustředěné v bootstrap modulu.

Související historie: [Aktivace Project Brain a uzavření PG-first runtime řezu](Historie%20projektu.md#2026-07-13---aktivace-project-brain-a-uzavření-pg-first-runtime-řezu)

## 2026-07-18 - AI taste bridge je datový kontrakt, ne náhrada lokálního scoringu

Rozhodnutí: větev `AI taste bridge` připravovat nejdřív jako lokální datový/API kontrakt pro samostatnou AI interpretaci vkusu, ne jako přímou integraci placeného ChatGPT API a ne jako vypnutí existujícího lokálního scoringu.

Důvod: lokální appka má být zdroj pravdy pro fakta, ratingy, seznamy, slovní hodnocení, identitu titulů a lokální signály. AI vrstva má nad těmito daty dělat interpretaci a návrhy, ale nemá přepisovat doménový stav ani skrývat původ signálu.

Důsledek: první implementační krok je read-only JSON endpoint a rozšíření `user_ratings` o slovní plus/mínus poznámky. Budoucí návraty od ChatGPT nebo jiné AI vrstvy se mají ukládat odděleně jako externí návrhy se zdrojem a vysvětlením, ne jako přímý přepis `genre_scores` nebo watchlistu.

Upřesnění: navazující AI projekt nemá dostávat jen seed tituly. Potřebuje také samostatný kontext o škálách, ručních preferencích a metodice lokálního scoringu. Proto má existovat oddělený `/api/ai/context` a plánovaný `/api/ai/scoring-explainer`; titulové endpointy se mají držet hlavně dat konkrétní sady titulů.

Související historie: [První backend řez AI taste bridge](Historie%20projektu.md#2026-07-18---první-backend-řez-ai-taste-bridge)

## 2026-07-18 - DuckDB cesta je kandidát na úplné odstranění

Rozhodnutí: cílový směr projektu je PostgreSQL-only. DuckDB už nemá být brán jako dlouhodobá runtime alternativa pro interaktivní appku.

Důvod: dosavadní časová měření a následné PG-first cleanupy opakovaně ukázaly, že DuckDB fallback je pro praktický provoz slepá větev. Udržování dvou runtime cest zvyšuje složitost, zpomaluje změny a vede k driftu schématu.

Důsledek: DuckDB se nema mazat impulzivně uprostřed rozpracovaného stavu. Nejprve je potřeba inventura zbylých závislostí: Python dependency, `db_bootstrap`, historický migrátor/export z DuckDB, testy, dokumentace a případné jednorázové obnovovací nástroje. Po inventuře má následovat plánovaný řez na odstranění nebo nahrazení PostgreSQL cestou.

Upřesnění: DuckDB větve už se nemají aktualizovat o nové funkce ani nové schéma. Když změna narazí na starou DuckDB cestu, správný směr je ji obejít přes PostgreSQL-only implementaci, ponechat starou větev beze změny jen pokud už není volaná, nebo ji plánovaně odstranit. Neprodlužovat život slepé větve další údržbou.

## 2026-07-19 - Role/postava je samostatný signál, ne rating titulu ani herce

Rozhodnutí: jemné pozitivní nebo negativní dojmy z konkrétní role/postavy ukládat do samostatné PostgreSQL tabulky `app.user_title_role_signals`, ne do celkového ratingu titulu a ne jako globální oblibu herce.

Důvod: případ typu Everwood/Ephram znamená „seriál celkově nízko, ale konkrétní postava, dialogy a chování jsou velmi silný pozitivní vzor“. Kdyby se to promítlo do ratingu seriálu, AI by si mohla odvodit špatný závěr, že má doporučovat podobné seriály. Kdyby se to promítlo do herce, pletla by se postava s osobou.

Důsledek: UI u `Main cast` má později rozlišit dvě akce: globální hodnocení herce/osoby a titulově vázané hodnocení role/postavy. AI endpointy mají tento signál předávat jako samostatnou vysvětlitelnou vrstvu.

## 2026-07-19 - FILMY je PostgreSQL-only

Rozhodnutí: starý souborový databázový backend už není ani fallback nebo údržbová vrstva v aplikačním kódu.

Důvod: po přechodu na PostgreSQL, AI endpointech a importu AI doporučení už udržování druhé runtime větve zvyšovalo riziko omylu a testy držely mrtvou cestu.

Důsledek: nové importy, rebuildy, maintenance akce i veřejné/navazující endpointy se mají dělat PostgreSQL cestou. Neobnovovat backend přepínače ani staré fallback větve. Historické migrační dokumenty mohou zůstat jako archiv, ale nejsou platný pracovní směr.

## 2026-07-19 - Akce na detailu zustavaji na detailu

Rozhodnutí: když je titul otevřený ze seznamu, formulářové akce na detailu mají po uložení vrátit uživatele zpět na stejný detail titulu, ne přímo do zdrojového seznamu.

Důvod: úprava detailu často probíhá ve více krocích, například nejdřív rating a potom přesun/kopie do jiného seznamu. Okamžitý návrat do seznamu by uživateli přerušil rozpracovanou editaci.

Důsledek: původní seznam se musí zachovat v breadcrumb/back kontextu a slouží pro návrat až po dokončení celkové editace detailu. Nevracet rating, slovní hodnocení, watched, clear rating ani copy-to-list akce přímo do zdrojového seznamu.
