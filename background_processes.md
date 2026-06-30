# Background procesy aplikace FILMY

## Aktivni provoz

### `metadata_pipeline`

Jediny bezny obsahovy background worker, ktery ma zustat v provozu.

Dela:
- doplnovani TMDB dat pro tituly, pokud se objevi nova prace
- materializaci `detail.json` pro tituly
- stahovani portretu osob
- materializaci `detail.json` pro osoby

Aktualni stav:
- po optimalizaci uz se netoci agresivne porad dokola
- kdyz neni co delat, prechazi do `idle_wait`
- znovu se probouzi po write akci z UI/API nebo po obcasne kontrolni periode

### `background supervisor`

Servisni dohled aplikace.

Dela:
- spusti `metadata_pipeline` pri startu aplikace
- hlida pad nebo zaseknuti workeru
- podle potreby ho znovu nahodi

Poznamka:
- neni to samostatny datovy worker
- patri do bezneho provozu

## Archivni servisni nastroje

### `tmdb_backfill`

Stary bulk backfill pro TMDB.

Rozhodnuti:
- nepatri do bezneho background provozu
- kod si nechavame jen jako archivni servisni nastroj
- v normalnim rezimu se nema pouzivat ani spoustet

### `title_details_cache`

Jednorazovy opravny mechanizmus pro starsi `detail.json` soubory vznikle pred zpresnenim metadata procesu.

Rozhodnuti:
- uz byl jednorazove pouzit jako opravny rewrite
- nepatri do bezneho background provozu
- nechavame ho jen jako archivni servisni zasah pro historicka data

### `refresh_stale_title_caches`

Starsi pomocny script souvisejici s opravami title cache.

Rozhodnuti:
- neni soucast standardni pipeline
- brat ho jako archivni servisni nastroj

## Zjednoduseny zaver

Pokud to shrnu:

- bezny provoz tvori `metadata_pipeline` + `background supervisor`
- vse ostatni jsou archivni nebo servisni nastroje
- tyto archivni nastroje nemaji byt beznou soucasti automatickeho background chovani aplikace
