# IMDb refresh

## Kontext
- data z imdb jsou staticka, kvůli imdb api
- IMDb je pro nas zakladni katalogova vrstva
- TMDB a dalsi veci jsou jen enrichment nad IMDb identitou

## Problem
- chci je obcas aktualizovat ale nechci to delat manuallne

## Soucasny stav
- musim stahnout dumpy z IMDb, rozbalit je a dat je do adresare `imdb/`
- potom je potreba, aby databaze nacetla nove TSV soubory do schema `raw`
- nasledne se musi obnovit katalogove tabulky a pohledy, ktere z `raw` ctou

## Co chceme zmenit
- chci aby to probehlo pres Nabidka (navbar) - System - IMDb Refresh
- proces musi byt bezpecny i ve chvili, kdy s aplikaci zrovna pracuji

## Navrh reseni

### 1. Zdroj dat

IMDb dumpy se stahuji z:
- `https://datasets.imdbws.com/`

Zajimaji nas hlavne:
- `title.basics.tsv.gz`
- `title.ratings.tsv.gz`
- `title.akas.tsv.gz`
- `title.episode.tsv.gz`
- `title.crew.tsv.gz`
- `title.principals.tsv.gz`
- `name.basics.tsv.gz`

### 2. Dvoufazovy refresh

Refresh by nemel zapisovat primo do aktivniho adresare `imdb/`.

Bezpecnejsi postup:

1. vytvorit docasny pracovni adresar, napr.:
   - `data/imdb_refresh/<timestamp>/download/`
   - `data/imdb_refresh/<timestamp>/extracted/`
2. stahnout vsechny `.gz` soubory do `download/`
3. rozbalit je do `extracted/`
4. overit, ze vsechny povinne TSV soubory existuji a nejsou prazdne
5. teprve potom provest kratky finalni swap do aktivniho `imdb/`

### 3. Finalni swap

Swap by mel byt co nejkratsi.

Navrh:

1. existujici `imdb/` prejmenovat na archivni adresar, napr.:
   - `data/imdb_archive/imdb_<timestamp>/`
2. pripraveny `extracted/` adresar prejmenovat nebo presunout na `imdb/`
3. spustit databazovy refresh nad novymi TSV
4. pokud refresh probehne uspesne, ponechat archiv pro pripadnou rychlou kontrolu nebo rollback
5. v nasi prvni verzi stary `imdb/` archivovat nechceme; po uspesnem switchnuti neni duvod ho drzet

### 4. Databazovy refresh

Po vymene souboru je potreba:

1. znovu vytvorit nebo obnovit `raw` pohledy nad novymi TSV
2. spustit `refresh_catalog()`
3. pripadne navazujici invalidaci / refresh lokalnich derived struktur

Prakticky to znamena:
- nechat existujici funkce pro `raw` a katalog, pokud uz to umi
- nepsat druhy paralelni mechanismus

### 5. Chovani za provozu

Nejvetsi riziko je okamzik, kdy appka zrovna cte stara IMDb data a my je menime.

Proto:

- download a rozbaleni musi bezet mimo aktivni `imdb/`
- jediny citlivy okamzik je finalni swap + refresh DB
- ten ma byt co nejkratsi a idealne pod nejakym explicitnim admin lockem

Moznosti:

#### Varianta A
- po dobu finalniho switche kratce zablokovat admin zapisove operace
- bezne cteni UI pokud mozno ponechat

#### Varianta B
- zobrazit v UI stav `IMDb refresh in progress`
- po dobu finalniho switche vratit kratkou servisni hlasku pro citlive endpointy

Za me:
- zacal bych jednoduchou variantou A

### 6. UI vrstva

V `System` pridat novou stranku `IMDb Refresh`.

Ta by mela ukazovat:
- posledni uspesny refresh
- jestli zrovna bezi download / extract / swap / db refresh
- log posledniho behu
- tlacitko `Start refresh`

Idealne i:
- kolik souboru uz je stazenych
- kolik jich bylo uspesne rozbaleno
- finalni vysledek

### 7. Background job

Tohle nema bezet jako dlouhy blokujici HTTP request.

Lepší model:
- uzivatelske kliknuti jen zalozi refresh job
- samotny IMDb refresh bezi jako jednorazovy background servisni proces
- stranka jen cte jeho stav a log

### 8. Logovani

Refresh by mel mit vlastni log, napr.:
- `data/imdb_refresh.log`

A mozna i stavovy JSON:
- `data/imdb_refresh_status.json`

To pomuze pro:
- UI
- ladeni
- pripadny restart nebo zotaveni po chybe

### 9. Chyby a rollback

Pokud selze:
- download
- rozbaleni
- kontrola souboru

pak se aktivni `imdb/` vubec nesmi menit.

Pokud selze:
- finalni DB refresh po switchnuti

pak jsou dve moznosti:
- bud vratit predchozi archiv jako rollback
- nebo ponechat novy `imdb/`, ale oznacit refresh jako chybovy a nechat dalsi krok na rozhodnuti

Za me:
- pro prvni verzi bych archiv stareho `imdb/` urcite nechal
- automaticky rollback bych zvazil az podle slozitosti

### 10. Co neudelat

- nestahovat primo do aktivniho `imdb/`
- neprepisovat jednotlive soubory za behu
- nedelat to jako dlouhy synchronni web request
- nemazat stare soubory hned na zacatku
- nezavadet druhy databazovy refresh mechanismus bokem od `refresh_catalog()`

## Otevrene otazky
- jak to udelat kdyz s programem prave pracuji?
- mozna nahradit ty stare obrazy az když budou kompletne stazene a rozbalene
- mozna na to pouzit nejaky docasny adresar

Dalsi otevrene otazky:
- chceme archiv stareho `imdb/` nechavat jen posledni jeden, nebo vice verzi?
- ma se po uspesnem IMDb refreshi automaticky probudit `metadata_pipeline`?
- ma UI nabizet i tlacitko `Validate only`, tedy jen stazeni a kontrolu bez finalniho switche?

Rozhodnuti:
- stary `imdb/` nechceme po refreshi zachovavat
- `metadata_pipeline` po IMDb refreshi neni nutne explicitne probouzet
- prvni verze nebude mit `Validate only`; bude jen jedna hlavni akce `Start refresh`

## Poznamky
- v textu je lepsi rikat `IMDb dumpy` nebo `TSV soubory`, ne `obrazy`
