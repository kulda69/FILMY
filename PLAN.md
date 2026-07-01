# PLAN

Pracovní plán pro `FILMY`.

Smysl:
- zapsat, co je další krok
- stručně vysvětlit proč
- držet směr projektu, aby se nerozjel jinam
- mít jedno místo, které můžu průběžně odškrtávat

Jak to používat:
- Jiří sem může dopsat další kroky, důvody i poznámky ke směru
- já budu položky průběžně odškrtávat a dopisovat krátký checkpoint
- když se směr změní, upraví se hlavně sekce `Směr`

## Směr

- Backend-first.
- Neřešit teď HTML/Jinja dřív, než bude dost pevný datový a API základ.
- Appka má být lokální zdroj pravdy pro historii, watchlist, ratingy a dostupnost.
- Výchozí vyhledávání titulů stavět nad IMDb katalogem.
- IMDb není online API zdroj, ale lokální katalog z pravidelně stahovaných dumpů.
- Stejný princip identity platí i pro osoby, nejen pro tituly.
- Nad jedním datovým základem může později existovat více různých pohledů do UI.
- Plex je doplňkový zdroj dat, ne primární katalogová vrstva.
- Vývoj běží na MacBooku, ale cílové běhové prostředí appky má být `mac-mini`, kde trvale běží Plex.
- TMDB enrichment není pro celý IMDb katalog.
- Dohledávat jen to, co je pro uživatele právě relevantní: watchlist, rozkoukané, viděné, ručně otevřené detaily a navazující lidi/související tituly.
- Systém musí umět spolehlivě průběžně dotahovat další informace na pozadí podle uživatelských akcí, ne jednorázově „nasypat všechno“.

## Aktuální stav

- [x] Základ FastAPI + DuckDB běží.
- [x] IMDb katalog se čte z lokálních TSV a materializuje do aplikačních tabulek.
- [x] Trakt a IMDb importy jsou oddělené od živých lokálních tabulek.
- [x] Sjednocená lokální knihovna je v `app.user_*` a `app.watch_events`.
- [x] TMDB sync pro jednotlivé tituly existuje.
- [x] Read-only napojení na Plex funguje proti skutečnému serveru `kulda_mini`.
- [x] První Plex sync do databáze proběhl.
- [x] Interní TMDB backfill a materializace detail cache běží pod background supervisorem.
- [ ] Ověřit stejnou stabilitu background pipeline i v reálném provozu mimo AI orchestrace.

## Další kroky

- [x] Ověřit, proč `data/tmdb_backfill.log` zůstal jen na startu, a rozhodnout, jestli backfill doběhl, spadl, nebo se vůbec nespustil.
  Důvod: runner tehdy nepsal průběžný stav po jednotlivých titulech, takže batch působil zasekle už po `batch_start`; nyní loguje per-item průběh a je vidět, kde se enrichment zpomaluje nebo selhává.

- [x] Navrhnout a zavést spolehlivý průběžný enrichment runner se stavem/checkpointy.
  Důvod: budoucí appka nebude dohledávat jen metadata titulů z watchlistu, ale i navazující informace o režisérech, hercích a jejich dalších filmech podle konkrétních uživatelských akcí.

- [x] Sepsat cílový backend workflow pro lokální správu knihovny.
  Důvod: ať je jasné, co přesně znamená `viděno`, `watchlist`, `rating`, `in progress` a co z toho se ukládá kam.

- [ ] Rozhodnout, které admin endpointy jsou jen dočasné technické nástroje a které zůstanou jako stabilní interní API.
  Důvod: backend už narostl a je dobré oddělit pracovní/debug vrstvu od budoucí app vrstvy.

- [ ] Sjednotit write orchestrace nad DuckDB a zamezit paralelním write procesům.
  Důvod: opakovaně narážíme na kolize write locku mezi server startem, background pipeline a jednorázovými maintenance scripty; problém není jen `async`, ale hlavně více nezávislých writerů nad jedním souborem.

- [ ] Ověřit přepínání `tmdb_primary_language` mezi `EN` a `CZ` na skutečné detail stránce.
  Důvod: config a čtecí vrstva jsou připravené, ale Jiří to zatím nechce ručně testovat; je potřeba později potvrdit, že se v UI správně přepíná primární jazyk a fallback.

- [ ] Přesunout scrollbar styling a další drobné UI helpery do společného shell CSS až později.
  Důvod: `Continue Watching` rail je teď funkční a stabilní; styling zatím necháváme tak, aby se do něj zbytečně nesahalo.

- [ ] Později navrhnout, jak `favorite_traits` vstoupí do doporučování a score.
  Důvod: nová vrstva typu `cerebral`, `dark`, `slow-burn` dává smysl jako jemnější preference než hrubé IMDb žánry, ale teď ji zatím jen sbíráme a nechceme předčasně vymýšlet výpočet.

- [x] Rozdělit akce v lokální nabídce na `Move to` a `Copy to`.
  Důvod: jeden titul může současně patřit do více uživatelských seznamů (`Watchlist`, `Koukni rychle`, `Mam`) a přesun by ho neměl automaticky mazat z původního seznamu tam, kde dává smysl kopie.

- [ ] Přidat později pohled „ukaž, co je ve více seznamech“.
  Důvod: duplicity mezi seznamy teď budou vědomě povolené a ručně spravované; později má smysl mít nad nimi kontrolní přehled.

- [ ] Doladit vazby mezi listy a doménové důsledky akcí typu `Watched`.
  Důvod: začínají se objevovat hraniční stavy mezi `Watchlist`, `Hot Watchlist`, `Watched` a dalšími seznamy; je potřeba sjednotit, co přesně která akce automaticky schovává, archivuje nebo nechává aktivní.

- [x] Zapsat a držet jednotné workflow pro detail titulu.
  Důvod: zatím hlavně sbíráme data; při dotazu na konkrétní titul musí appka okamžitě vědět, co všechno má dohledat, složit a ukázat jako jeden smysluplný detail.

- [x] Navázat z detailu titulu na detail osoby přes `Main cast`.
  Důvod: data i portréty lidí se už materializují na pozadí; další přirozený krok je udělat z herců klikatelné vstupy do znovupoužitelného detailu osoby.

- [~] Rozřezat `filmy/db.py` na menší moduly podle odpovědnosti.
  Důvod: soubor už je příliš velký na bezpečné úpravy; první krok je nechat `filmy.db` jako stabilní fasádu a přesouvat implementace po ověřených blocích do menších modulů.
  Checkpoint: hotový je modul pro osoby (`filmy/db_people.py`), modul lokální knihovny (`filmy/db_library.py`) a modul legacy zdrojů (`filmy/db_legacy.py`). Do legacy modulu už jsou přesunuté nejen veřejné Trakt/IMDb/Plex read+sync vstupy, ale i interní helpery `_sync_trakt_*`, `_sync_imdb_*` a `_sync_plex_*`. `filmy/db.py` teď funguje hlavně jako fasáda a drží shared helpery plus seed/migrační logiku.

## Rozhodnutí

- IMDb a Trakt importy jsou historický seed, ne hlavní zdroj pravdy.
- Živé uživatelské údaje mají být lokálně v appce.
- IMDb je základní katalogová vrstva pro vyhledávání, protože má nejširší pokrytí titulů.
- Primární identita titulu v systému je IMDb `tconst` typu `tt5611024`.
- Primární identita osoby v systému je IMDb `nconst` typu `nm0000233`.
- Plex může dodat další vrstvu lokálních dat o knihovně, přehrávání a stavu sledování, ale má zůstat jen doplňkovým ingest zdrojem.
- Plex integraci navrhovat s ohledem na to, že produkční běh nebude na stejném stroji jako vývoj, ale na `mac-mini`.
- TMDB metadata a providery bereme jako enrichment nad lokální knihovnou.
- TMDB není primární zdroj pro nalezení titulu, ale druhá vrstva po nalezení IMDb záznamu.
- Praktická výjimka: protože IMDb nemá přímé online API v tomto workflow, může se pomocně ověřovat existence titulu i v TMDB, ale vazba a hlavní identita má zůstat přes IMDb ID.
- Totéž platí pro režiséry, herce a další osoby: TMDB může dodat enrichment osoby, ale hlavní interní identita má zůstat přes IMDb `nconst`.
- Enrichment je demand-driven, ne plošný nad celým IMDb katalogem.
- Uživatelova akce nad detailem může vyvolat další enrichment frontu, například pro režiséra, herce a jejich související filmografii.
- Potřebujeme robustní background mechanismus pro průběžné dohledávání dat, protože tento vzor bude v appce opakovaný a dlouhodobý.
- Priorita je vyřešit logický a datový základ fungování; UI je až vrstva nad stabilní identitou, vazbami a enrichment workflow.
- Plex má smysl připojit i dřív jako pomocný zdroj podkladů pro mapování titulů a následný TMDB enrichment.
- Obrázky držet na filesystemu, ne v databázi.
- Prioritní jazyk TMDB je `en-US`, fallback `cs-CZ`.
- Primární jazyk obsahu a detailu je angličtina; čeština je doplňkový fallback tam, kde dává smysl.

## Workflow detailu titulu

- Vstup je konkrétní titul identifikovaný primárně přes IMDb `tconst`.
- První krok je vždy načíst lokální detail z katalogu a lokální knihovny, ne začínat dotazem na externí API.
- Pokud už lokálně existují katalogová, knihovní a TMDB data, detail se hned složí z dostupných informací bez čekání.
- Pokud některá enrichment data chybí, detail se má i tak zobrazit hned z toho, co už máme, a paralelně se má vyvolat background dohledání chybějících částí.
- Detail titulu má při běžném uživatelském dotazu skládat aspoň tyto bloky:
  - základní identita: název, originální název, typ, rok, runtime, žánry, rating, aliasy
  - lidský obsahový detail: stručné „o čem to je“, hlavní herci, tvůrce / režie, případně epizody nebo sezóny
  - lokální stav uživatele: viděno, naposledy viděno, watchlist, rating, zařazení v seznamech, případně progress
  - dostupnost a média: providery v ČR, poster, backdrop a další lokální assety
- Výchozí čtecí jazyk detailu je angličtina. Pokud existuje užitečný český doplněk, může se zobrazit vedle nebo jako fallback, ale nemá přepsat anglický základ.
- Detail titulu musí fungovat i jako textový datový výstup bez UI. To je pracovní minimální forma, podle které se později postaví skutečné rozhraní.
- Z detailu titulu se teprve odvozují navazující background úkoly: lidi, související tituly, další filmografie a podobně.
- Dotaz na titul tedy není jen „najdi řádek v databázi“, ale „slož nejlepší aktuální detail z lokálních dat a současně rozhodni, co ještě chybí a má se dohledat“.

## Budoucí pohledy nad daty

- Jeden z budoucích pohledů může být vztahový, inspirovaný staršími filmovými databázemi typu „stojím na filmu -> vidím režiséra a herce -> stojím na režisérovi -> vidím jeho další filmy -> stojím na filmu -> vidím další herce“.
- Tento pohled je ale jen jedna možná navigační vrstva nad databází, ne jediná budoucí forma UI.
- Vedle něj mohou existovat i jiné, méně systematické nebo více task-oriented pohledy, například obyčejné hledání, watchlist, historie, doporučení nebo detail jednoho titulu.

## Doplňkové zdroje

- Plex je doplňkový ingest zdroj.
- Krátkodobě dává smysl zkusit připojení už teď, aby přinesl další podklady pro mapování a TMDB dohledávání.
- Dlouhodobě může mít dvě role:
  - jednorázový nebo občasný bootstrap import knihovny/stavu sledování
  - průběžný sync nových událostí, například přes webhooky
- Ani při napojení Plexu se nemá měnit hlavní identita objektů v systému: tituly přes IMDb `tconst`, osoby přes IMDb `nconst`.

## Poznámky Jiřího

- tady v tom souboru máme napsano "Z detailu titulu se teprve odvozují navazující background úkoly: lidi, související tituly, další filmografie a podobně."
- dohledavani vsech informaci bude podle me velmi pomale. pomale = nepouzitelne
- měření ukázalo, že `detail.json` cache už pomáhá, ale hlavní brzda je pořád fuzzy search vrstva; později má smysl optimalizovat search, ne detail cache
# K uvaze a posouzeni
- co kdyby se infomrace jak jak se prubezne dohledavaji pro rychlost zobrazeni informaci zapisovaly do lokalniho souboru.
- je to takova mala inspirace soubory nfo ktere se drive pouzivaly
- pri vyhledani nazvu filmu (tak jak jsme implementovali i s moznymi preklepy) vime kod filmu podle imdb ve formatu tt.....
- stejne tak se jmenuji adresare v data - assets - tmdb
- ve stejnem adresari by se mohl vytvorit novy sobour s veškerymi detaly titulu
- ja bych navrhoval ve formatu dictonary nebo neco podobneho
- napriklad u rezie by byl samozrejme reziser, ale byl by uveden jako tuple ("nm0357333", "Paul Hamann")
- stejne tak u herců
- to by bylo obdobne uvedeno i ve strukture ktera se bude posilat sablone jinja2
- podle me je vyhoda v tom, ze staci najit spravny nazev a dalsi udaje si vytahnout z tohoto soubru přičemž:
  - pri kliknutí na rezisera už by se nehledalo jeho jmeno, bylo by hned zname jeho id podle imdb
  - pri kliknuti na herce by platilo to same
  - takze pri hledani co jeste stejny reziser natočil uz by se to hledalo jen podle jeho identifikatoru
- pripada mi ze touto cestou by se to dalo zrychlit.
- dohledavani informaci a vytvareni techto souboru by mohlo beze na pozadi po dlouhou dobu
- pri hledani filmu u ktereho tento pomocny soubor neexistuje by se to proste vyhledalo poprve
- u lokalnich uzivatelskych seznamu se zacina chovani narovnavat vic systemove:
  `Watched` ma byt chapano jako doménová akce, ne jen technický zápis `watch_event`
- aktualni prakticka konvence:
  `Watched` v UI znamena pridat titul do seznamu `Viděl jsem` a zaroven ho odebrat z prave otevreneho aktivniho seznamu
- `Viděl jsem` bereme jako alias pro user-facing watched list; nenechavat to jen jako domluvu v chatu, ale drzet to explicitne v kodu a workflow
- u seznamu jsme nově přidali `description`; vlevo se zobrazuje misto technickeho `list_kind` a vpravo pod nazvem vybraneho seznamu
- zalozeni noveho listu je pres minimalisticke `+` v hlavicce `Your Lists`; vytvoreny list se ma hned stat aktivnim vybranym seznamem
- editace popisu existujiciho seznamu patri do hlavicky prave vybraneho seznamu vpravo; `Edit` ma byt zarovnane horizontalne vedle badge s nazvem
- lokalni nabidka na subkartach se kvuli orezu scroll kontejneru presunula z `details` na nativni HTML `popover`; pokud se k tomu budeme vracet, neresit to uz jen posunem `top/bottom`
- dalsi systemovy krok pozdeji:
  vytahnout chovani `Watched` do jedne centralni servisni/doménové akce, aby vedlejsi dusledky nebyly roztrousene po UI routach
- po vizualni kontrole skutecne domovky bereme aktualni vysku prave sekce `watchlist` jako minimalni; pri dalsich upravach ji uz nezmensovat
- v domovce ma byt prava sekce s vybranym seznamem vzdy alespon na tomto minimu a zaroven dorovnana na vysku `Your Lists`, pokud je levy panel vyssi

## Checkpoint

- 2026-07-01: do vyhledávání přidána interní recall/cache vrstva `app.search_recall`. Title i person lookup teď nejdřív zkouší malé mapování nedávných dotazů na konkrétní `tt...` / `nm...`, teprve potom pouští plné vyhledávání. Limit je v `config.toml` jako `search_recall_limit = 500`. Je to čistě technická optimalizace pro opakované hledání, ne uživatelská funkce a nemá mít vlastní UI.
- 2026-06-30: search dostal skutečnou HTML výsledkovou stránku `GET /search` napojenou na horní navbar; stránka umí zobrazit jak nalezený titul, tak nalezenou osobu a shortlist alternativ.
- 2026-06-30: title lookup už nepreferuje lokálně viděné tituly; z rankingu zmizel `watched_count`, protože hledání musí fungovat i pro věci, které ještě nejsou v uživatelových seznamech ani historii.
- 2026-06-30: title search nově bere výrazněji v úvahu aliasy a normalizaci lokálních názvů; dotazy typu `3 procenta` se mapují i na názvy se znakem `%`.
- 2026-06-30: person search se rozšířil z pouhého full-name matchingu na víc variant jména. Fuzzy a levenshtein vrstva teď pracují i s příjmením a s kompaktní variantou bez mezer, takže jednoslovný překlep typu `Gylenhal` už najde `Jake Gyllenhaal`.
- 2026-06-30: person lookup už pro shortlist kandidátů netahá plnou filmografii každé osoby; místo toho používá levnější `credit_count` agregaci z `app.title_credits`. Další krok, pokud se k tomu vrátíme, je hlavně změřit rychlost celé search pipeline na reálných dotazech a případně ještě zúžit DB shortlist před Python fuzzy scoringem.
- 2026-06-30: aktivní Trakt integrace byla vyřazena z běžné appky. Admin endpointy a CLI export už nejsou součástí standardního provozu; export helper byl přesunut do `filmy.legacy.trakt_export` a historické `old.trakt_*` tabulky zůstávají jen jako archivní vrstva.
- 2026-06-30: FastAPI entrypoint byl rozdělen podle odpovědnosti. `filmy.main` je teď jen sestavení aplikace + `lifespan`, sdílené helpery jsou v `filmy.app_shared` a routy jsou rozdělené do `filmy.routers.web`, `filmy.routers.ui` a `filmy.routers.api`.
- 2026-06-30: proběhl krátký dokumentační pass nad novou FastAPI strukturou a složitější helper logikou. Breadcrumb/navigation helpery, cache warmup a nové person-affinity funkce mají doplněné vysvětlující docstringy, aby další změny nestály jen na implicitní znalosti chatu.
- 2026-06-29: detail titulu je znovu složený do tří vrstev pod hero blokem. `Main cast` je zpátky jako samostatný panel, `Directed by` a `Written by` mají vlastní nadpisy a badge jména, `Created by` je pod tím, aliasy jsou omezené jen na `en/cs/es/de` a nadpisy sekcí používají stejnou barvu jako název filmu.
- 2026-06-29: přidána druhá ruční preference vrstva `favorite_traits` pro volné výrazy typu `cerebral`, `dark` nebo `slow-burn`; je dostupná v `System` jako samostatná stránka a zatím slouží jen ke sběru explicitních preferencí bez navazující výpočetní logiky.
- 2026-06-27: vytvořen sdílený `PLAN.md` v rootu projektu jako jedno místo pro další kroky a průběžné odškrtávání.
- 2026-06-27: doplněn důležitý směr pro enrichment. Neobohacujeme celý IMDb katalog, ale jen uživatelsky relevantní tituly a navazující entity vyvolané konkrétní akcí v appce.
- 2026-06-27: potvrzeno, že výchozí hledání titulů má stát na IMDb katalogu; TMDB je až navazující enrichment vrstva.
- 2026-06-27: upřesněno, že primární vazba titulu je přes IMDb `tconst`, zatímco TMDB může pomocně potvrdit existenci nebo dodat enrichment, ale nenahrazuje hlavní identitu.
- 2026-06-27: doplněno, že stejný princip platí i pro osoby: interní identita přes IMDb `nconst`, TMDB jen jako doplňková enrichment vrstva.
- 2026-06-27: zapsán návrhový princip budoucího vztahového pohledu nad databází, ale jen jako jedna z více možných UI vrstev.
- 2026-06-27: zapsán Plex jako doplňkový ingest zdroj, který má smysl zkusit připojit už v backendové fázi kvůli bohatším podkladům pro mapování a enrichment.
- 2026-06-27: ověřeno připojení na skutečný Plex server `kulda_mini`; detail metadat vrací GUID vazby na IMDb i TMDB, takže Plex může dodat použitelné mapovací podklady pro FILMY.
- 2026-06-27: upřesněno, že projekt se vyvíjí na MacBooku, ale cílově poběží na `mac-mini`, kde zároveň běží Plex; integraci je potřeba držet přenositelnou mezi stroji.
- 2026-06-27: první Plex sync doběhl do DB. Importováno 1193 Plex položek, z toho 402 s IMDb ID, 399 s TMDB ID; do `Plex Library` se propsalo 405 položek a vzniklo 100 `watch_events` ze stavu sledování.
- 2026-06-27: upraveno PyCharm datasource schema mapping v `.idea/dataSources.local.xml` na explicitní schémata `app`, `old`, `raw`, `main`, protože se vracela IDE hláška kolem `filmy.app.catalog_episodes.getImportedKeys` a duplicitních reportů. Další krok po restartu projektu: ověřit, jestli hláška zmizela; pokud ne, udělat čistý reinit datasource.
- 2026-06-27: protože hláška `filmy.app.catalog_episodes.getImportedKeys` přetrvala i po restartu, vyčištěna i PyCharm datasource historie a introspection cache v `.idea/dataSources/`; v databázi samotné `app.catalog_episodes` žádné foreign keys nemá, takže problém dál vypadá jako IDE metadata stav, ne DuckDB schéma.
- 2026-06-27: detail titulu už má funkční textový výstup nad vyhledáním podle názvu i `tconst`; IMDb osoby a kredity se nově materializují do `app.catalog_people` a `app.title_credits`, takže po výměně TSV dumpů v `imdb/` stačí znovu spustit `refresh_catalog()` a detail se přestaví nad novými daty.
- 2026-06-27: detail titulu má lokální cache v `data/assets/tmdb/<tconst>/detail.json`; při opakovaném zobrazení se bere z disku a přepíše se jen když se změní zdrojový fingerprint, takže běžný detail je rychlý a zároveň se dá znovu dopočítat po aktualizaci dat.
- 2026-06-27: rychlostní test ukázal, že cache detailu funguje, ale celkový dotaz stále brzdí vyhledávací vrstva; další optimalizace má proto cílit na fuzzy lookup a shortlist kandidátů.
- 2026-06-27: přidán malý runner `filmy-materialize-title-details`, který projde jen kompletní tituly a zapíše jim `data/assets/tmdb/<tconst>/detail.json`; použitelné pro jednorázové doplnění cache nebo pozdější refresh.
- 2026-06-28: domovka má limit 50 položek pro vybraný seznam a odkazy `Show all` vedou na samostatnou stránku seznamu; detail prezentace titulu je navíc cacheovaný v procesu přes bounded `lru_cache`, s invalidací po zápisech do lokalních dat.
- 2026-06-27: materializace `detail.json` už neběží jen nad perfektně kompletními TMDB záznamy, ale nad všemi relevantními tituly; existující cache se validuje přes fingerprint místo slepého přeskočení podle existence souboru.
- 2026-06-27: TMDB backfill nově umí označit `not_found` tituly a vyřadit je z dalších batchů, takže se nemá opakovaně zasekávat na stejných IMDb ID, která v TMDB nejsou.
- 2026-06-27: do appky přidán interní background supervisor. Při startu FastAPI spouští TMDB backfill a materializaci `detail.json`, hlídá jejich liveness podle procesu a log activity a po pádu nebo zaseknutí je znovu nahodí.
- 2026-06-27: přidán lookup flow `GET /api/catalog/lookup`, který pro dotaz na název vrací rovnou vybraný detail titulu a zároveň shortlist kandidátů pro budoucí UI přepnutí mezi shodami.
- 2026-06-27: při budování Jinja2 vyhledávacího UI zachovat tolerantní hledání na překlepy typu `tennet` -> `Tenet`; uživatel nemá být závislý jen na přesném názvu.
- 2026-06-27: později doplnit testy pro fuzzy lookup a doladění hran podobnosti; současný kód pro tolerantní hledání už existuje a je funkční.
- 2026-06-27: po znovuotevření projektu a nové introspekci se duplicitní hláška v PyCharm objevuje dál; protože běh appky ani DB logika nejsou blokované, evidujeme to zatím jen jako odložený IDE-only technický dluh k pozdějšímu dořešení.
- 2026-06-27: zpřehledněn TMDB backfill runner. Nově loguje průběh po jednotlivých titulech místo tichého čekání jen na konec batch, takže je vidět, kde přesně se enrichment zpomaluje nebo selhává.
- 2026-06-27: zpevněna TMDB enrichment vrstva. Mapping se neoznačuje jako `synced` před uložením payloadů, `/configuration` se v procesu kešuje, asset fetch umí vrátit částečný výsledek s per-asset chybami a retry vrstva zachytává i timeouty / nízkoúrovňové I/O chyby.
- 2026-06-27: repo prošlo prvním strukturálním úklidem směrem k běžné FastAPI appce. Hlavní logika je v balíčku `filmy/`, integrace jsou v `filmy/integrations/`, runner skripty v `filmy/scripts/` a root obsahuje už jen tenké vstupní wrappery a projektové soubory.
- 2026-06-27: proběhl druhý úklidový pass nad artefakty v rootu. Dočasné `tmp_trakt*.json` přesunuty do `tmp/`, smazány cache a `.DS_Store`, a `.gitignore` doplněn o generované debug/probe soubory a lokální pracovní adresáře.
- 2026-06-27: ověřeno, že TMDB backfill runner sám o sobě umí běžet stabilně, ale detached start přes AI exec prostředí nebyl spolehlivý. Až bude UI, je potřeba výslovně otestovat běh bez AI orchestrace: normální proces, interní worker nebo systémový launcher, a potvrdit, že background enrichment funguje stabilně i v reálném provozu appky.
- 2026-06-28: doplněny lokální user listy o `description`, minimalistické vytvoření nového listu přes `+`, a inline `Edit` pro popis aktivního seznamu vpravo.
- 2026-06-28: `Watched` v kartovém menu je nyní explicitně navázané na seznam `Viděl jsem`; akce titul odebere z aktivního seznamu a zároveň ho přidá do watched alias listu.
- 2026-06-29: lokální alias seznam `Viděl jsem` byl zrušen jako samostatný user list; místo něj je nově systémový pohled `Watched` odvozený přímo z `watch_events`, zatímco `Recently Watched` zůstává jen časově omezený výřez nad stejnou historií.
- 2026-06-29: u uživatelských seznamů nově držíme doménové rozlišení mezi budoucím `Move to` a `Copy to`; například `Watchlist` -> `Koukni rychle` nebo `Mam` nemá automaticky znamenat odebrání z původního seznamu.
- 2026-06-29: lokální popover menu na kartách seznamů nově používá dvoukrokový flow `Move to` / `Copy to`; hlavní menu zůstává krátké a cílové seznamy se ukazují až ve druhém kroku, bez nabídky aktuálního seznamu.
- 2026-06-29: při hromadném refreshi stale title cache jsme znovu narazili na write lock kolizi nad `filmy.duckdb`; potvrzený technický dluh je potřeba sjednotit write model a nepouštět více nezávislých writerů paralelně.
- 2026-06-28: lokální nabídka subcard už není bootstrap/details varianta; používá nativní `popover`, protože scroll panel watchlistu nabídku jinak ořezával.
- 2026-06-28: vizuálně ověřeno na skutečném renderu domovky, že aktuální výška pravé sekce `watchlist` je spodní použitelná hranice; další úpravy ji mohou zvětšit, ale ne zmenšit.
- 2026-06-28: `config.toml` je zdroj pravdy pro limity `continue_watching_limit` a `my_lists_selected_limit`; změna souboru se má propsat bez další úpravy kódu.
- 2026-06-28: v domovce je pravý panel s vybraným seznamem dorovnaný na výšku `Your Lists`, ale současně nesmí klesnout pod ověřené minimum.
- 2026-06-30: geometrie homepage seznamů se má řídit obsahem levého panelu `Your Lists`; pravý panel se zvoleným seznamem se na desktopu dorovnává na stejnou výšku, ale minimálně musí pojmout zhruba 3 řady filmových karet.
- 2026-06-30: homepage sekce nahoře s `Your Lists` a vybraným seznamem je po doladění schválená a má se brát jako zmrazená proti dalším úpravám; výjimkou je jen samostatná sekce `Suggestions`, kterou lze dál měnit odděleně.
- 2026-06-30: `metadata_pipeline` už nemá běžet agresivně každé 2 sekundy i bez práce. Nově má aktivní krátký sleep jen při skutečném postupu, jinak přechází do idle wait režimu s delší periodickou kontrolou a probuzením přes signal soubor po write akcích z UI/API.
- 2026-06-30: background provoz je zjednodušený na `metadata_pipeline` + `background supervisor`. Staré `tmdb_backfill`, `title_details_cache` rewrite a `refresh_stale_title_caches` se mají brát už jen jako archivní servisní nástroje, ne jako běžné background procesy.
- 2026-06-30: přidána první verze `System > IMDb Refresh`. Je to jednorázový servisní subprocess se stavem a logem, který stáhne IMDb TSV dumpy bokem, rozbalí je do `data/imdb_refresh/<timestamp>/`, krátce přepne aktivní `imdb/` a pak spustí standardní `refresh_catalog()`.
- 2026-06-28: pro osoby vzniká stejný materializovaný detail princip jako pro tituly: lokální `detail.json` pod `data/assets/people/<nconst>/detail.json`, klíčovaný přes IMDb `nconst`, aby budoucí UI detail osoby nečekal na skládání dat za běhu.
- 2026-06-28: metadata pipeline nově po TMDB a title detail cache průběžně materializuje i person `detail.json`, takže i detaily lidí se mají doplňovat na pozadí stejným supervised loopem.
- 2026-06-28: background pipeline nově dohledává i TMDB portréty lidí přes IMDb `nconst` a ukládá je do `data/assets/people/<nconst>/portrait.*`; navazující person `detail.json` pak umí hned nést `portrait_url`.
- 2026-06-28: detail titulu nově zobrazuje u `Main cast` malé portréty z lokálních person assetů; při otevření detailu se chybějící portréty herců dohledávají přednostně na pozadí.
- 2026-06-28: `Main cast` se po otevření detailu průběžně sám obnovuje malým lokálním partial refreshem, takže nově stažené portréty naskakují bez ručního reloadu celé stránky.
- 2026-06-29: `Main cast` chipy na detailu titulu nově vedou na HTML detail osoby `/people/{nconst}`; detail osoby se skládá z materializovaných person dat a filmografie a linkuje zpět do detailů titulů přes IMDb identitu bez dalšího fuzzy lookup mezikroku.
- 2026-06-30: horní `Back` navigace je nahrazená delšími breadcrumbs. Stopa se nově přenáší v URL mezi homepage/listy, detailem titulu a detailem osoby, takže se dá vracet i přes několik mezikroků zpět bez rozbití layoutu.
