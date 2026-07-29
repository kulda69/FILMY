# Navrh: list actions a title session

Tento soubor je navrh doménového modelu pro akce nad titulem a pro vztahy mezi
seznamy. Neni to hotove rozhodnuti ani hotova implementace.

Cil:

- nehardcodovat chovani podle jednotlivych dnesnich nazvu seznamu,
- neztracet kontext, kdyz Jiri pri praci s jednim titulem odboci na detail osoby
  nebo na jinou stranku a pak se vrati,
- vyhodnocovat vedlejsi dusledky az nad celym balickem zmen, ne po prvnim
  jednotlivem kliknuti.

## Proc nestaci okamzite akce

Jednoducha predstava `klik = okamzity finalni stav` narazi na realne workflow:

1. Jiri stoji na detailu titulu.
2. Zada rating.
3. Odejde na herce nebo jinou souvisejici stranku.
4. Vrati se na titul.
5. Udela `Copy to Kouknout znovu`.

Kdyby uz krok 2 okamzite definitivne spustil vsechny doménové dusledky
(`watched`, archivace shortlistu, presuny mezi listy), je vysoke riziko, ze
se dalsi kroky budou tlouct s uz jednou uzavrenym stavem.

Proto je bezpecnejsi oddelit:

- lokalni zmeny na titulu,
- finalni vyhodnoceni dusledku,
- aplikaci pravidel mezi seznamy.

## Navrhovany model

### 1. Title session

Pri vstupu na detail titulu vznikne pracovni kontext nad jednim `tconst`.

Tento kontext ma reprezentovat:

- nad jakym titulem se prave pracuje,
- z jakych seznamu nebo stranky se na nej prislo,
- jake zmeny uz byly v aktualni session provedeny,
- co je zatim jen navrh/draft a co je uz ulozene.

Pracovni nazev:

- `title_session`

Minimalni obsah session:

- `session_id`
- `tconst`
- `opened_from`
- `return_to`
- `pending_actions`
- `pending_membership_changes`
- `pending_state_changes`
- `started_at`
- `updated_at`

### 2. Pending actions

Jednotlive uzivatelske kroky se nejdriv zapisou jako male akce do session.

Priklady:

- `set_rating`
- `mark_watched`
- `copy_to_list`
- `move_to_list`
- `add_to_list`
- `remove_from_list`
- `set_notes`

Tyto akce zatim samy o sobe neznamenaji, ze uz probehlo finalni doménové
vyhodnoceni vsech vedlejsich dusledku.

### 3. Finalize

Teprve pri uzavreni session se z pending akci spocita finalni stav titulu.

Pracovni nazev:

- `finalize_title_session`

Pri finalize se deje:

1. slouceni vsech pending akci do jednoho finalniho zameru,
2. dopocteni odvozenych dusledku,
3. aplikace pravidel mezi seznamy,
4. aplikace vyjimek,
5. ulozeni finalniho stavu.

## Tri vrstvy pravidel

### 1. Primarni zamer uzivatele

To je to, co Jiri opravdu chtel udelat:

- ohodnotit titul,
- oznacit ho jako watched,
- presunout ho do jineho seznamu,
- zkopirovat ho do jineho seznamu.

### 2. Odvozene dusledky

To jsou systemove efekty, ktere mohou vzniknout automaticky:

- rating implikuje watched,
- watched ma archivovat nektere planning listy,
- explicitni move ma odstranit clenstvi ze zdrojoveho seznamu,
- copy nema odstranit clenstvi ze zdrojoveho seznamu.

### 3. Vyjimky a preserve pravidla

To jsou pravidla, ktera maji vyssi prioritu nez obecne chovani:

- `Kouknout znovu` se ma po watched ponechat,
- `Plex Library` se ma po watched ponechat,
- explicitni `copy_to Kouknout znovu` se nema prepsat naslednou archivaci z
  obecnych planning roli.

## Navrh role-based modelu seznamu

Nazev konkretniho seznamu nema byt hlavni logika. Hlavni logika se ma vazat na
roli seznamu.

Prvni navrh roli:

- `planned`
- `shortlist`
- `rewatch`
- `owned_local`
- `external_suggestion`
- `negative`
- `ignore`

Priklad mapovani dnesnich seznamu:

- `Watchlist` -> `planned`
- `Koukni rychle` -> `shortlist`
- `Kouknout znovu` -> `rewatch`
- `Plex Library` -> `owned_local`
- `AI navrhy` -> `external_suggestion`

Tohle mapovani je zatim navrh, ne zafixovane rozhodnuti.

## Prvni sada scenaru

### Watched

1. `Watched` nad titulem ve `Watchlist`
   - pridat do watched vrstvy
   - odebrat z `planned`

2. `Watched` nad titulem v `Koukni rychle`
   - pridat do watched vrstvy
   - odebrat ze `shortlist`

3. `Watched` nad titulem v `Plex Library`
   - pridat do watched vrstvy
   - ponechat v `owned_local`

4. `Watched` nad titulem v `Kouknout znovu`
   - pridat do watched vrstvy
   - ponechat v `rewatch`

### Rating

5. `Rate` nad titulem v `Koukni rychle`
   - ulozit rating
   - z ratingu odvodit watched
   - po finalize odebrat ze `shortlist`

6. `Rate` nad titulem v `Watchlist`
   - ulozit rating
   - z ratingu odvodit watched
   - po finalize odebrat z `planned`

7. `Rate` nad titulem v `Plex Library`
   - ulozit rating
   - z ratingu odvodit watched
   - ponechat v `owned_local`

### Move a copy

8. `Move to`
   - odebrat ze zdrojoveho seznamu
   - pridat do ciloveho seznamu
   - bez dalsich vedlejsich efektu, pokud nejsou explicitne definovane

9. `Copy to`
   - ponechat ve zdrojovem seznamu
   - pridat do ciloveho seznamu
   - bez dalsich vedlejsich efektu

10. `Rate` a pak `Copy to Kouknout znovu`
   - ulozit rating
   - odvodit watched
   - ponechat v `rewatch`
   - odstranit jen ty membershipy, ktere spadaji do bezne planning/shortlist
     vrstvy

## Co z toho plyne pro TOML

Budou potreba aspon tri skupiny konfigurace:

1. mapovani seznam -> role
2. pravidla akce -> dusledky
3. preserve/vyjimky s vyssi prioritou

Pracovni obrys:

```toml
[lists.watchlist]
role = "planned"

[lists.koukni-rychle]
role = "shortlist"

[lists.kouknout-znovu]
role = "rewatch"

[lists.plex-library]
role = "owned_local"

[actions.watched]
set_watched = true
archive_roles = ["planned", "shortlist"]
keep_roles = ["rewatch", "owned_local"]

[actions.rated]
set_rating = true
derived_actions = ["watched"]

[actions.move_to]
remove_from_source = true
add_to_target = true

[actions.copy_to]
remove_from_source = false
add_to_target = true
```

Tohle ale jeste nestaci na priority a kontext. Proto je nejspis potreba i
vrstva preserve/override pravidel.

## Dulezite otevrene otazky

1. Kdy se session finalizeuje?
   - explicitni `Save`
   - odchod z detailu
   - timeout
   - navrat z breadcrumbu

2. Ktere akce se maji ukladat hned a ktere az pri finalize?
   - rating mozna muze jit ulozit hned jako draft
   - vztahy mezi seznamy by se mely resit az pri finalize

3. Co je kanonicky zdroj pravdy pro watched?
   - watch event
   - content state
   - derived marker z ratingu

4. Jak se ma chovat `Plex Library`?
   - dnes je to spis signal `mam lokalne`
   - neni to bezny planning list
   - ma mit vlastni preserve chovani

## Doporuceny dalsi krok

Nepsat hned TOML parser ani runtime engine.

Nejdriv sepsat konkretni tabulku scenaru pro skutecne dnesni seznamy:

- akce
- zdrojovy seznam
- cilovy seznam
- watched efekt
- co odstranit
- co ponechat
- poznamka

Jakmile bude sada realnych scenaru kompletnejsi, pujde z ni bezpecne odvodit:

- stabilni role seznamu,
- struktura TOMLu,
- hranice mezi `pending actions` a `finalize`.
