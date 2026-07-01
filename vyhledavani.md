# Vyhledávání

Tento soubor popisuje, jak v projektu dnes funguje vyhledávání titulů a osob. Je to pracovní technický dokument pro další rozšiřování, ladění a zapisování nových rozhodnutí.

## 1. Základní princip

Vyhledávání stojí primárně na lokálním IMDb katalogu v DuckDB.

- Základní titulová tabulka: `app.catalog_titles`
- Epizody: `app.catalog_episodes`
- Osoby: `app.catalog_people`
- Kredity: `app.title_credits`
- Aliasy titulů: `app.title_aliases`

Hlavní identita je IMDb:

- titul: `tconst` ve formátu `tt...`
- osoba: `nconst` ve formátu `nm...`

TMDB v této vrstvě neslouží jako vyhledávací základ. Je jen enrichment vrstva pro postery, overview, česká metadata a dostupnost.

## 2. Co dnes existuje

### Rychlá recall vrstva

Než se spustí plné lookup vyhledávání, appka se nejdřív dívá do pomocné tabulky `app.search_recall`.

Smysl:

- urychlit opakované hledání stejného titulu nebo osoby
- pamatovat si i varianty dotazu, včetně překlepů nebo zkomolenin
- při nalezení přímé mapy vrátit rovnou detail cíle bez celého fuzzy pipeline průchodu

Tabulka drží:

- původní hledaný text
- normalizovaný `query_key`
- typ cíle (`title` / `person`)
- cílové IMDb ID (`tt...` / `nm...`)
- poslední známé fuzzy skóre
- počet použití a čas posledního použití

Velikost je omezená konfigurací:

- `search_recall_limit` v `config.toml`
- výchozí hodnota je `500`

Chování:

- pokud se v `app.search_recall` najde odpovídající mapování, lookup ho použije jako první
- pokud se nic nenajde, běží normální titulová nebo person lookup pipeline
- do recall vrstvy se zapisují jen smysluplné, dostatečně jisté výsledky
- recall zápis je jen optimalizace; při locku databáze nesmí shodit samotné vyhledávání

### Tituly

V kódu jsou dnes dvě odlišné cesty:

1. `search_catalog(...)`
   - jednoduché substring hledání přes `ILIKE`
   - vhodné pro prostý seznam výsledků
   - řazení je víc katalogové než "inteligentní"

2. `lookup_title_by_query(...)`
   - hlavní tolerantní lookup
   - umí překlepy a vícekrokové dohledání
   - nejdřív kontroluje `app.search_recall`
   - vrací:
     - vybraný titul
     - shortlist kandidátů
     - metadata o matchi

### Osoby

Podobně existuje:

1. `lookup_person_by_query(...)`
   - tolerantní lookup osob
   - pracuje nad `app.catalog_people`
   - nejdřív kontroluje `app.search_recall`
   - vrací vybranou osobu i kandidáty

## 3. API endpointy

Aktuální veřejné interní endpointy v `filmy/main.py`:

- `GET /api/catalog/search`
  - jednoduché katalogové hledání titulů
- `GET /api/catalog/describe`
  - vrací přímo detail vybraného titulu podle dotazu
- `GET /api/catalog/lookup`
  - hlavní lookup flow pro tituly
- `GET /api/catalog/lookup/text`
  - textový výstup vybraného titulu
- `GET /api/catalog/person/lookup`
  - lookup osoby
- `GET /api/catalog/person/lookup/text`
  - textový výstup osoby

Poznámka k UI:

- v UI má být vyhledávání zásadně dostupné přes horní navbar
- v `templates/base.html` tam dnes skutečně je:
  - textové pole `q`
  - tlačítko `Go`
  - druhé tlačítko pro širší fuzzy režim
- navbar už je napojený na skutečnou HTML search stránku:
  - `GET /search`
  - parametr `mode` rozlišuje běžnější flow a širší fuzzy flow
- prakticky to znamená, že dnešní vyhledávání už není jen API experiment, ale má i reálnou Jinja2 výsledkovou stránku

## 4. Jednoduché vyhledávání titulů

Funkce: `search_catalog(query, title_type, limit)`

Chování:

- hledá přes:
  - `primary_title ILIKE '%query%'`
  - `original_title ILIKE '%query%'`
- umí volitelně filtrovat `title_type`
- vrací základní katalogová pole:
  - `tconst`
  - `title_type`
  - `primary_title`
  - `original_title`
  - `start_year`
  - `runtime_minutes`
  - `genres`
  - `average_rating`
  - `num_votes`
- ke každému výsledku se dotahuje i lokální knihovní souhrn přes `_fetch_library_summary(...)`

Řazení je dnes:

1. tituly s ratingem před tituly bez ratingu
2. vyšší `average_rating`
3. vyšší `num_votes`
4. novější `start_year`
5. `primary_title`

Tohle není "chytré" vyhledávání, spíš prostý katalogový filtr.

## 5. Hlavní lookup titulů

Funkce: `lookup_title_by_query(query, title_type, candidates_limit)`

Tohle je důležitější cesta. Cíl je:

- uživatel napíše název filmu nebo seriálu
- nemusí trefit přesný název
- může mít drobný překlep
- výsledkem má být jeden nejlepší kandidát a zároveň shortlist dalších možností

Flow má dnes čtyři vrstvy.

### 5.0 Nultá vrstva: search recall

Nejprve se kontroluje `app.search_recall`.

Pokud tam pro daný dotaz už existuje dříve ověřené mapování:

- vrátí se rovnou dříve nalezený titul
- obejde se katalogový substring search i fuzzy výpočet
- při úspěšném použití se záznam znovu označí jako čerstvě použitý

To je určené hlavně pro opakované dotazy typu:

- stejné jméno osoby hledané vícekrát za sebou
- stejný titul hledaný opakovaně
- stejný překlep, který už jednou správně vedl na konkrétní výsledek

### 5.1 První vrstva: přímé substring hledání

Funkce: `_search_catalog_for_lookup(...)`

SQL logika:

- hledá v `primary_title` a `original_title` přes `ILIKE`
- filtruje volitelný `title_type`

Řazení preferuje:

1. přesná shoda `lower(primary_title) = lower(query)`
2. přesná shoda `lower(original_title) = lower(query)`
3. prefixová shoda `primary_title ILIKE query || '%'`
4. prefixová shoda `original_title ILIKE query || '%'`
5. ostatní substring shody

Pak se ještě dorovnává:

- novější rok
- existující rating
- vyšší rating
- více hlasů
- název

Tohle bývá nejrychlejší cesta, pokud uživatel zadá rozumně přesný název.

### 5.2 Druhá vrstva: fuzzy kandidáti

Funkce: `_search_catalog_for_lookup_fuzzy(...)`

Spouští se pokud:

- dotaz má víc tokenů, nebo
- nebyli nalezeni žádní kandidáti, nebo
- přímí kandidáti nevypadají dostatečně přesvědčivě

Rozhodovací funkce:

- `_should_expand_to_fuzzy(...)`

Pravidlo dnes:

- pokud mezi top kandidáty není přesná nebo article-less shoda, počítá se podobnost
- když nejlepší přímé skóre klesne pod `0.72`, rozšiřuje se vyhledávání do fuzzy režimu

Fuzzy SQL kandidáti:

- pracují nad normalizovaným match key
- berou v úvahu `primary_title` i `original_title`
- filtrují kandidáty podle:
  - stejných prvních 3 nebo 2 znaků
  - podobné délky názvu
- z databáze vezmou maximálně 500 kandidátů

Pak se nad nimi v Pythonu dopočítá `fuzzy_score`.

### 5.3 Třetí vrstva: široký levenshtein fallback

Funkce: `_search_catalog_for_lookup_levenshtein(...)`

Použije se jen když:

- dotaz má víc tokenů
- a vybraný kandidát po předchozích krocích není dostatečně jistý

Rozhodovací funkce:

- `_is_confident_lookup(...)`

Lookup se považuje za jistý pokud:

- je přesná shoda primary/original title
- nebo article-less přesná shoda
- nebo `fuzzy_score >= 0.82`

Když jistota nestačí:

- použije se širší výběr podle:
  - stejného prvního písmene
  - podobné délky
  - `levenshtein(...)`
- z DB se vezme až 500 kandidátů
- pak se znovu seřadí podle `fuzzy_score`, watched signálu, hlasů a roku

## 6. Jak se počítá podobnost názvu

Hlavní funkce:

- `_best_title_similarity(...)`
- `_token_similarity_score(...)`

### Normalizace

Funkce: `_normalize_match_key(...)`

Normalizace dnes dělá:

- trim
- lowercase
- odstranění roku v závorce, např. `(1999)`
- nahrazení `&` za `and`
- odstranění diakritiky přes Unicode normalizaci
- odstranění nealfanumerických znaků
- zkolabování mezer

Volitelně umí i:

- odstranit úvodní anglické členy `the`, `a`, `an`

Stejný princip existuje i v SQL přes `_duckdb_match_key_sql(...)`.

### Výpočet skóre

Na jednu variantu názvu se bere:

- sekvenční podobnost přes `difflib.SequenceMatcher`
- tokenová podobnost po slovech

Pro víceslovné dotazy:

- výsledné skóre = `0.6 * sequence_score + 0.4 * token_score`

Pro jednoslovné dotazy:

- bere se lepší z obou hodnot

Bonus:

- pokud jedna normalizovaná varianta začíná druhou, nebo naopak, skóre se minimálně zvedne na `0.8`

Tokenová podobnost:

- pro každý token v dotazu se hledá nejpodobnější token ve variantě
- zprůměrují se dílčí skóre
- pokud jsou tokeny ve správném pořadí jako subsequence, přičítá se bonus `0.05`

## 7. Jak se vybírá vítězný titul

Funkce: `_pick_best_title_match(...)`

Pravidla dnes:

1. pokud existují fuzzy kandidáti a nejlepší má `fuzzy_score >= 0.72`, bere se on
2. jinak se hledají přesné shody nad normalizovaným názvem
3. pokud je víc přesných shod, preferuje se:
   - lepší alias priorita
   - více IMDb hlasů
   - novější rok
4. pokud nic z toho nevyhraje, vezme se první kandidát z už seřazeného seznamu

Záměrně tam už není lokální bias podle toho, co mám viděné nebo ve vlastních seznamech.

- hledání má fungovat i pro tituly, které ještě vůbec nemám v lokálních datech
- `watched_count` se z lookup rankingu odstranil

## 8. Co vrací title lookup

`lookup_title_by_query(...)` vrací:

- `query`
- `title_type`
- `selected_tconst`
- `selected`
  - plná title presentation z `get_title_presentation(...)`
  - obohacená o `match`
- `candidates`
  - shortlist kandidátů
- `candidate_count`

Každý kandidát nese mimo jiné:

- `tconst`
- `primary_title`
- `original_title`
- `title_type`
- `kind_label`
- `start_year`
- `runtime_minutes`
- `genres`
- `average_rating`
- `num_votes`
- `library`
- `is_selected`
- `is_exact_match`
- `fuzzy_score`

## 9. Vyhledávání osob

Hlavní funkce: `lookup_person_by_query(...)`

Logika je obdobná jako u titulů, ale se speciálním důrazem na jméno a příjmení.

### První vrstva

`_search_people_for_lookup(...)`

- `primary_name ILIKE '%query%'`
- řazení:
  - přesná shoda jména
  - vyšší `credit_count`
  - novější `birth_year`
  - jméno

### Druhá vrstva

`_search_people_for_lookup_fuzzy(...)`

- kandidáty předvybírá přes normalizované jméno i jeho části:
  - celé jméno
  - první token
  - poslední token
  - kompaktní varianta bez mezer
- prefix 3 nebo 2 znaky
- podobná délka celého jména nebo příjmení
- limit 500 kandidátů
- dopočet `fuzzy_score` nad více variantami jména
- filtr `fuzzy_score >= 0.65`

### Třetí vrstva

`_search_people_for_lookup_levenshtein(...)`

- stejné první písmeno nad celým jménem nebo jeho tokeny
- podobná délka
- DB `levenshtein` nad:
  - celým jménem
  - příjmením
  - kompaktní variantou bez mezer
- znovu dopočet a seřazení podle `fuzzy_score`

### Výběr vítěze

`_pick_best_person_match(...)`

Pravidla:

1. fuzzy vítězí při `fuzzy_score >= 0.72`
2. jinak přesná shoda jména
3. mezi přesnými shodami se preferuje:
   - vyšší `credit_count`
   - novější `birth_year`

Jednoslovné překlepy typu `Gylenhal` se proto už nesmí vyhodnotit jen přes celé jméno.

- důležitá je i shoda na příjmení
- skóre se počítá přes více variant:
  - celé jméno
  - jméno bez mezer
  - jednotlivé tokeny
  - zejména poslední token jako příjmení

Jistý lookup osoby:

- přesná shoda jména
- nebo `fuzzy_score >= 0.82`

## 10. Vazba lookupu na detail

Lookup nekončí jen na katalogové řádce.

### Tituly

Po výběru vítěze se bere:

- `get_title_presentation(selected["tconst"])`

To znamená, že finální vybraný titul už není jen "nalezený řádek", ale plná prezentační vrstva používaná i pro detail stránky.

### Osoby

Po výběru vítěze se bere:

- `get_person_presentation(selected["nconst"])`

Stejně tak osoba jde přes prezentační vrstvu a materializovaná data.

## 11. Důležité současné limity a vlastnosti

### 11.1 IMDb je zdroj pravdy pro search

To je záměr.

- IMDb má nejširší pokrytí
- `tconst` / `nconst` jsou hlavní identita
- TMDB není rozhodovací vrstva pro match

### 11.2 Epizody nejsou hlavní search surface

Vyhledávací logika je stavěná hlavně pro:

- filmy
- TV movies
- seriály
- minisérie

Epizody se řeší až v detailu seriálu a v navazujících lokálních operacích.

### 11.3 Fuzzy vrstva je dnes známá brzda

To už je v projektu dříve zaznamenané.

- `detail.json` cache pomohla
- ale hlavní výkonnostní problém zůstal ve fuzzy lookup vrstvě

To znamená:

- další výkonové ladění má směřovat spíš sem než do detail cache

### 11.4 Search UI ještě není hotový produkt

Dnešní stav:

- backend logika vyhledávání existuje
- API existuje
- existuje i HTML/Jinja výsledková stránka `GET /search`
- stále je ale prostor pro další UX ladění, hlavně:
  - práce s alternativními kandidáty
  - rychlost
  - lepší rozlišení, zda uživatel hledá titul nebo osobu

## 12. Historicky důležité rozhodnutí

Na začátku projektu padlo důležité rozhodnutí:

- výchozí vyhledávání titulů stavět nad IMDb katalogem
- tolerantní hledání má zvládat i drobné překlepy typu `tennet -> Tenet`
- po nalezení názvu potřebujeme především spolehlivě zjistit IMDb identitu `tt...`

To platí dál.

## 13. Co má smysl dopsat později

Tento soubor je záměrně otevřený pro další doplňování.

Budoucí kandidáti na rozšíření:

- reálný HTML search flow z navbaru
- UI pro přepínání mezi shortlist kandidáty
- přesnější pravidla pro preferenci filmu vs seriálu při stejnojmenných výsledcích
- práce s aliasy a lokálními názvy ve fuzzy score výrazněji než dnes
- smysluplné smíšené vyhledávání titul vs osoba v jednom výsledkovém toku
- testy pro fuzzy lookup a regresní sadu typických překlepů
- měření výkonu po jednotlivých vrstvách lookupu
- případné pozdější embedding / semantic search experimenty, ale až jako další fáze, ne místo IMDb lookupu

## 15. Aktuální checkpoint

- `GET /search` dnes umí zobrazit jak titulový výsledek, tak osobu.
- Title lookup používá aliasy titulů a normalizaci češtiny bez diakritiky.
- Normalizace umí i převod `%` <-> `percent` <-> `procenta`, takže dotaz typu `3 procenta` může najít `3%`.
- U osob se nyní už zohledňuje i příjmení a varianta bez mezer; to opravilo hledání typu `Gylenhal` -> `Jake Gyllenhaal`.
- Person lookup už při shortlistu netahá plnou filmografii pro každého kandidáta; používá levnější `credit_count` agregaci z `app.title_credits`.
- Další rozumný krok bude výkonové měření nad reálnými dotazy a případné zúžení DB shortlistu ještě před Python fuzzy scoringem.

## 14. Praktické poznámky pro další práci

Když budeme vyhledávání dál měnit, je dobré sem pokaždé doplnit:

- co přesně se změnilo
- proč se to změnilo
- jestli šlo o přesnost, výkon nebo UX
- jaké konkrétní dotazy byly problematické
- jestli jde o změnu pro tituly, osoby nebo obojí
