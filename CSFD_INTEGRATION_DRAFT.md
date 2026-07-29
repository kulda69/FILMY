# Navrh: ČSFD jako paralelni ceska katalogova vrstva

Tento soubor je ciste teoreticky navrh. Neimplementuje scraping ani nepridava
novy zdroj pravdy do runtime.

Cil:

- pojmenovat, jakou roli by mohla mit ČSFD v projektu FILMY,
- oddelit ceskou lokalni katalogovou vrstvu od IMDb-centric modelu,
- nepredstirat, ze je technicky stejne snadna jako IMDb.

## Proc o ČSFD vubec uvazovat

Pro cesky a slovensky kontext ma ČSFD nekolik vyhod:

- je lokalne prirozena,
- ma ceske nazvy a ceske obsahy,
- ma lokalne relevantni VOD dostupnost,
- muze mit lepsi pokryti nekterych ceskych/slovenskych titulu nebo televiznich
  formatu, ktere IMDb nema nebo nema dost dobre pouzitelne.

Zaroven ČSFD zjevne pracuje i s typy obsahu, ktere nejsou jen bezny
mezinárodní film/serial katalog:

- `TV film`
- `pořad`
- `studentský film`
- `amatérský film`
- `divadelní záznam`
- `hudební videoklip`
- `koncert`

To je pro ceskou vrstvu potencialne cenne.

## Proc z ČSFD neudelat prostou nahradu IMDb

I kdyz domenove dava ČSFD smysl, technicky ma jina omezeni:

- verejny pristup je vice chraneny,
- bot/anti-scraping ochrany zneprijemnuji stabilni backend ingest,
- neni potvrzene zadne verejne API pro stejny styl integrace jako mame kolem
  IMDb dumpu a TMDB enrichmentu.

Z toho plyne:

- ČSFD dava smysl jako silna paralelni vrstva,
- ale ne jako bezbolestna jedina primarni identita celeho systemu.

## Doporuceny koncept

Ne `IMDb nebo ČSFD`, ale:

- `IMDb globalni kostra`
- `ČSFD ceska lokalni vrstva`

To znamena:

- IMDb muze zustat hlavni mezinárodní identita tam, kde existuje,
- ČSFD muze byt rovnocenna ceska reference,
- nektere tituly mohou byt `csfd-only`,
- nektere budou `dual-linked`.

## Navrhovane identity

Titul by do budoucna mohl mit tyto identity:

- `imdb_tconst`
- `csfd_id`
- `tmdb_id`

Ne vsechny musi existovat vzdy.

Prvni navrh stavu:

- `imdb-linked`
- `csfd-linked`
- `dual-linked`
- `tmdb-linked`

Prakticky:

- bezny americky film bude casto `imdb-linked` + `dual-linked` po sparovani s
  ČSFD,
- nektery cesky televizni nebo okrajovy titul muze byt `csfd-linked` bez IMDb,
- TMDB zustane enrichment vrstva, ne hlavni lokalni ceska autorita.

## Co by se z ČSFD vytezilo

### 1. Ceske nazvy a lokalni alternativy

ČSFD by mohlo byt silny zdroj pro:

- cesky nazev,
- lokalni alternativni nazvy,
- lokalni rozliseni mezi ceskym/slovenskym nazvem a puvodnim nazvem.

### 2. Ceske obsahy

To je pro FILMY zajimave jak pro lidsky detail, tak pro pozdejsi AI interpretaci
v cestine.

### 3. Lokalne relevantni VOD

ČSFD ma vlastni vrstvu `VOD`, ktera muze byt pro ceske prostredi velmi cennym
doplnenim vedle TMDB/JustWatch.

### 4. Tituly mimo beznou IMDb osu

Sem spadaji hlavne:

- ceske TV filmy,
- porady,
- studentske filmy,
- starsi nebo lokalne specificke kusy,
- jine formaty, ktere by FILMY jednou mohl chtit evidovat.

## Co z toho zatim neplyne

Z tohoto navrhu zatim neplyne:

- ze se ma menit hlavni identita cele db na ČSFD,
- ze se ma hned scrapovat celá ČSFD,
- ze se ma kazdy titul povinne párovat na ČSFD,
- ze se ma menit dnesni PostgreSQL schema.

## Doporuceny doménovy model

Prvni bezpecny smer je zavest pojem:

- `parallel catalog authority`

Mozni kandidati:

- `imdb`
- `csfd`

To by znamenalo:

- katalogovy zaznam muze mit vic autorit,
- appka nema nutit vsechny tituly do jedne jedine identity,
- ceske tituly mohou mit kvalitni lokalni vrstvu i kdyz globalni identita neni
  idealni.

## Navrhovany minimalni teoreticky datovy obrys

Ne finální schema, jen koncept:

### authority link

- `title_id`
- `authority`
- `authority_key`
- `authority_url`
- `is_primary_for_locale`
- `matched_by`
- `matched_at`

Priklady:

- `authority = imdb`, `authority_key = tt14203808`
- `authority = csfd`, `authority_key = 1132029`

### localized metadata

- `authority`
- `title`
- `original_title`
- `title_type`
- `country`
- `year`
- `runtime`
- `summary_cs`
- `vod_sources`

## Jak by se to chovalo v appce

Na detailu titulu by se jednou mohlo zobrazovat:

- `IMDb`
- `ČSFD`
- `TMDB`

Ale kazda z techto vrstev by mela jinou roli:

- IMDb: globalni identita a zakladni mezinárodní kostra
- ČSFD: ceska lokalni vrstva a lokalni VOD/context
- TMDB: artwork, overview, provideri, enrichment

## Jak to souvisi s vyhledavanim

ČSFD je zajimava i pro budoucí ceske lookupy.

Prvni rozumna domenova predstava:

- vyhledani titulu muze zacit v IMDb,
- ale pro cesky dotaz muze byt uzitecne i ČSFD candidate lookup,
- vysledek se pak ma sloucit, ne přepisovat.

Tedy ne:

- `pouzij IMDb nebo ČSFD`

Ale:

- `najdi kandidaty z vic autorit a pak je resolverem spoj`

## Otevrene otazky

1. Ma byt v ceskem locale nekdy `csfd` primarni zobrazena autorita?
2. Jak se bude resit `csfd-only` titul bez IMDb?
3. Jak se budou sparovavat dual-linked tituly?
4. Ma byt ČSFD ingest jen rucni na vyzadani podle URL?
5. Jak moc ma FILMY rozsireni na `porad`, `TV film`, `studentsky film` a
   podobne formaty?

## Doporuceny dalsi krok

Zatim nedelat implementaci.

Nejdřív držet tento teoreticky model:

- IMDb neni jedina mozna autorita,
- ČSFD neni jen doplnkovy link, ale potencialni ceska katalogova vrstva,
- technicky ale potrebuje opatrnejsi pristup nez IMDb/TMDB.

Prakticky dalsi navrhova etapa:

1. rozhodnout, zda FILMY vubec chce podporovat `csfd-only` tituly,
2. rozhodnout, ktere typy obsahu mimo klasicky film/serial chteji byt v rozsahu,
3. teprve potom resit manualni nebo poloautomaticke parovani `IMDb <-> ČSFD`.
