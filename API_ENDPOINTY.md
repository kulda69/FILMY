# API endpointy pro navazujici projekty

Tento soubor popisuje stabilizovane nebo pripravovane endpointy, ktere maji
pouzivat jine lokalni projekty. Neni to dump vsech internich admin rout.

Pri zmene verejneho/navazujiciho endpointu aktualizovat tento soubor ve stejnem
rezu jako kod. Projekt `/Volumes/not_inserted/AI/filmy-knihy` ho muze brat jako
kontrakt.

## Doporucene poradi pro externi AI projekt

Externi AI projekt nema zacinat rovnou titulovymi endpointy. Nejdrive si ma
nacist kontext a vysvetleni scoringu, jinak muze spatne interpretovat ratingy,
lokalni skore nebo nove role/postava signaly.

Doporucene poradi volani:

1. `GET /api/ai/context`
   - hodnotici skaly,
   - `Favorite Genres`,
   - `Favorite Traits`,
   - definice `people_affinity` a `title_role_signals`.
2. `GET /api/ai/scoring-explainer`
   - co znamena lokalni scoring,
   - rozdil mezi Jiriho ratingem, IMDb ratingem a lokalnim score,
   - proc `title_role_signals` zatim nejsou zapocitane do `final_score`.
3. `GET /api/ai/taste-inputs`
   - sirsi vstupy podle `ai_input_role`,
   - `external_suggestion` a `ignore` jsou vyloucene z AI vstupu.
4. Volitelne `GET /api/ai/rated-titles`
   - silne lokalne hodnocene tituly od zadaneho prahu.
5. Volitelne `GET /api/ai/taste-seed`
   - jeden konkretni seznam, kdyz je potreba cilenejsi sada prikladu.

Bez prvnich dvou kroku nema externi AI projekt delat zavery typu „tento titul je
celkove oblibeny“ jen z jednoho score nebo jednoho signalu.

## GET `/api/ai/context`

Stav: implementovano.

Read-only endpoint pro obecny kontext, ktery si navazujici AI projekt muze
volat pred praci s konkretnimi filmy. Ma byt maly a stabilni; nevraci seznam
titulu.

### Ucel

- vysvetlit hodnotici skaly,
- predat cele `Favorite Genres`,
- predat cele `Favorite Traits`,
- popsat, jak cist lokalni score signaly,
- dat navazujicimu projektu kontext "kde se pohybujeme" bez nutnosti pokazde
  tahat seed tituly.

### Priklad volani

```bash
curl 'http://127.0.0.1:8019/api/ai/context'
```

Pri jinem dev portu nahrad `8019` aktualnim portem FastAPI serveru.

### Struktura odpovedi

```json
{
  "contract_version": 1,
  "rating_scales": {
    "user_rating": {
      "min": 1,
      "max": 10,
      "type": "integer",
      "description": "Jiriho hodnoceni titulu v lokalni appce."
    },
    "person_affinity_rating": {
      "min": 0,
      "max": 10,
      "type": "integer",
      "description": "Jiriho oblibenost osoby; 0 znamena bez pozitivni affinity."
    },
    "title_role_signal_strength": {
      "min": 0,
      "max": 10,
      "type": "integer",
      "description": "Sila konkretniho signalu role/postavy v jednom titulu; neni to celkovy rating titulu ani affinity k herci."
    },
    "imdb_rating": {
      "min": 0,
      "max": 10,
      "type": "decimal",
      "description": "Externi IMDb rating, neni to Jiriho hodnoceni."
    },
    "favorite_preference_rank": {
      "min": 1,
      "max": 10,
      "type": "integer",
      "description": "Rucni priorita favorite genres/traits; nizsi cislo znamena silnejsi preferenci, null znamena nehodnoceno."
    }
  },
  "favorite_genres": [
    {
      "genre": "Drama",
      "weight": 1.0,
      "preference_rank": 1,
      "source_origin": "local_app",
      "source_ref": "system.favorite_genres",
      "notes": null,
      "is_active": true,
      "created_at": "2026-06-29T12:00:00",
      "updated_at": "2026-07-18T12:00:00"
    }
  ],
  "favorite_traits": [
    {
      "trait": "slow-burn",
      "weight": 1.0,
      "preference_rank": 1,
      "source_origin": "local_app",
      "source_ref": "system.favorite_traits",
      "notes": null,
      "is_active": true,
      "created_at": "2026-06-29T12:00:00",
      "updated_at": "2026-07-18T12:00:00"
    }
  ],
  "score_signal_notes": {
    "genre_score_signals": "Lokalni preference zanru podle historie, ratingu a dalsich signalu.",
    "actor_affinity_rating": "Souhrnny signal oblibenosti hodnocenych hercu navazanych na titul.",
    "people_affinity": "Konkretni osoby z titulu, ktere maji rucni affinity rating; kontrakt pro navazujici rozsireni taste-seed.",
    "title_role_signals": "Konkretni role/postavy v titulu, ktere Jiri oznacil jako pozitivni, negativni nebo smisene signaly. Tento signal je oddeleny od ratingu titulu i affinity k herci."
  },
  "title_role_signal_definitions": {
    "signal_types": {
      "character": "Postava jako celek.",
      "dialogue": "Dialogy, hlas, slovni projev nebo zpusob komunikace postavy.",
      "behavior": "Chovani, rozhodovani a reakce postavy.",
      "relationship_dynamic": "Vztahova dynamika postavy s ostatnimi.",
      "performance": "Herecke provedeni v konkretni roli.",
      "visual_appeal": "Vzhled, styl nebo vizualni pusobeni role v danem titulu.",
      "attraction": "Pritazlivost nebo charisma role v danem titulu a dobe.",
      "other": "Jiny titulove vazany signal role/postavy."
    },
    "polarities": {
      "positive": "Signal, ktery muze podporit podobna doporuceni.",
      "negative": "Signal, ktery muze podobna doporuceni oslabit nebo vyloucit.",
      "mixed": "Signal je dulezity, ale neni jednoznacne pozitivni ani negativni."
    },
    "notes": "Poznamka je textovy kontext pro cloveka a pozdeji AI interpretaci; nema se sama prevadet na ciselne skore bez opatrnosti."
  },
  "usage_notes": [
    "Endpoint je read-only a nevola externi AI ani online katalogy.",
    "Navazujici AI projekt ho ma volat jako obecny kontext pred praci s konkretnimi tituly.",
    "Favorite genres a favorite traits se vraci cele, vcetne neaktivnich polozek.",
    "Title role signals mohou byt silne i u titulu s nizkym celkovym hodnocenim; napr. nebrat cely serial jako oblibeny, ale brat konkretni postavu/dialogy/chovani jako vzor."
  ]
}
```

## GET `/api/ai/scoring-explainer`

Stav: implementovano.

Read-only endpoint pro vysvetleni, jak FILMY pocita lokalni score a jak ma
navazujici AI projekt cist jednotlive signaly. Je to zakladni kontextovy
endpoint podobny `/api/ai/context`, ale nevraci preference ani tituly; vraci
metodiku.

### Ucel

- vysvetlit rozdil mezi lokalnim scoringem, IMDb ratingem a Jiriho ratingem,
- popsat, z ceho vznikaji `genre_score_signals`,
- vysvetlit vyznam `watch_signal_score`, `rating_signal_score`,
  `actor_affinity_score` a `final_score`,
- popsat, jak se do score zapocitavaji `Favorite Genres` a jak opatrne cist
  `Favorite Traits`,
- vysvetlit, ze `title_role_signals` jsou zatim samostatna nova vrstva mimo
  `final_score` a `genre_score_signals`,
- dat ChatGPT/navazujicimu projektu textovy navod, jak signaly interpretovat,
  ne jen syrova cisla.

### Priklad volani

```bash
curl 'http://127.0.0.1:8019/api/ai/scoring-explainer'
```

### Struktura odpovedi

```json
{
  "contract_version": 1,
  "score_scope": "default",
  "status": {
    "implemented_scoring": "Aktualni lokalni scoring pocita hlavne zanrove a titulove signaly z historie, lokalnich ratingu, watch signalu, people affinity a rucnich favorite genres.",
    "role_signals_status": "Title role signals jsou nova samostatna vrstva. Zatim se nepositaji do genre_score_signals ani final_score.",
    "future_role_signal_task": "Pozdeji navrhnout samostatnou scoring vetev pro role/postava signaly, napr. role_signal_score nebo character_preference_signals. Nezvedat tim automaticky celkove hodnoceni titulu."
  },
  "principles": [
    "Lokalni score je pomocny signal pro razeni a vysvetleni, ne definitivni pravda.",
    "Jiriho lokalni rating ma vyssi vyznam nez externi IMDb rating.",
    "IMDb rating je verejny externi signal kvality/popularity, ne osobni preference.",
    "People affinity je osobni signal k osobe, ne obecna popularita herce.",
    "Title role signals mohou byt silne i u titulu s nizkym celkovym ratingem; cist je samostatne."
  ],
  "signals": {
    "final_score": {
      "meaning": "Normalizovane lokalni skore kandidata nebo zanru v danem score scope.",
      "ai_usage": "Pouzit jako podpurny signal razeni, ne jako jediny duvod doporuceni."
    },
    "title_role_signals": {
      "meaning": "Konkretni signaly role/postavy v jednom titulu.",
      "ai_usage": "Cist samostatne mimo final_score.",
      "current_scoring_inclusion": false
    }
  },
  "known_limitations": [
    "Title role signals zatim nejsou zapocitane do final_score ani genre_score_signals.",
    "Favorite traits jsou sbirane jako jemny kontext; jejich vliv na scoring se muze dal menit."
  ],
  "recommended_ai_reading_order": [
    "Nejdrive nacist /api/ai/context kvuli skalam a definicim.",
    "Potom nacist /api/ai/scoring-explainer kvuli vyznamu score poli.",
    "Potom nacist /api/ai/taste-inputs pro sirsi vstupy podle ai_input_role."
  ]
}
```

### Vzdaleny ukol

Pozdeji navrhnout, jestli a jak maji `title_role_signals` vstupovat do
lokalniho scoringu. Nemaji mechanicky zvedat celkove hodnoceni titulu; spravnejsi
pravdepodobne bude samostatna vetev typu `role_signal_score` nebo
`character_preference_signals`.

## GET `/api/ai/taste-seed`

Stav: implementovano, payload se bude jeste rozsirovat o dalsi filtry.

Read-only endpoint pro predani lokalnich prikladu vkusu samostatne AI vrstve,
napriklad projektu `/Volumes/not_inserted/AI/filmy-knihy`.

Endpoint nevola ChatGPT API, nehleda online a nemeni lokalni stav. Vraci pouze
fakta a lokalni signaly z FILMY databaze.

### Query parametry

- `source_list`: id, slug nebo nazev seznamu, ze ktereho se maji vybrat priklady.
  Vychozi hodnota je `kouknout-znovu`.
- `limit`: pocet vracenych polozek, minimum `1`, maximum `200`, vychozi `50`.
- `min_user_rating`: planovany filtr pro lokalni rating od zadane hodnoty vcetne.
- `notes`: planovany filtr pro slovni hodnoceni; napr. `any`, `liked`,
  `disliked`.

Poznamka: aktualni lokalni seznam ma slug `kouknout-znou` a nazev `Kouknout znou`.
Endpoint pro pohodli podporuje alias `kouknout-znovu`.

### Priklad volani

```bash
curl 'http://127.0.0.1:8019/api/ai/taste-seed?source_list=kouknout-znovu&limit=2'
```

Pri jinem dev portu nahrad `8019` aktualnim portem FastAPI serveru.

### Struktura odpovedi

```json
{
  "source_list": {
    "query": "kouknout-znovu",
    "found": true,
    "id": "custom-list-...",
    "slug": "kouknout-znou",
    "name": "Kouknout znou",
    "description": "stojí za to si znovu pustit",
    "list_kind": "custom",
    "ai_input_role": "strong_positive"
  },
  "limit": 2,
  "items": [
    {
      "imdb_id": "tt0242795",
      "tconst": "tt0242795",
      "tmdb_id": 27099,
      "title": "Come Undone",
      "original_title": "Presque rien",
      "title_type": "movie",
      "year": 2000,
      "genres": ["Drama", "Romance"],
      "imdb_rating": 6.7,
      "imdb_votes": 5655,
      "user_rating": 7,
      "liked_notes": null,
      "disliked_notes": null,
      "rated_at": "2026-07-10T12:41:52.102601",
      "actor_affinity_rating": null,
      "people_affinity": [
        {
          "nconst": "nm0000206",
          "name": "Keanu Reeves",
          "credit_group": "cast",
          "ordering": 1,
          "affinity_rating": 9,
          "is_favorite": true
        }
      ],
      "title_role_signals": [
        {
          "signal_key": "role-signal:tt0379623:nm0395777:ephram-brown:dialogue",
          "nconst": "nm0395777",
          "person_name": "Gregory Smith",
          "character_name": "Ephram Brown",
          "signal_type": "dialogue",
          "polarity": "positive",
          "strength": 10,
          "notes": "Silny duvod dokoukani; dialogy a chovani postavy.",
          "source_origin": "local_app",
          "source_ref": "manual_role_signal:tt0379623",
          "updated_at": "2026-07-19T18:20:00"
        }
      ],
      "genre_score_signals": [
        {
          "genre": "Drama",
          "final_score": 67.9766,
          "rating_signal_score": 0.0492,
          "watch_signal_score": 0.5669,
          "actor_affinity_score": 0.5217
        }
      ]
    }
  ]
}
```

### Vyuziti pro druhy projekt

Druhy projekt by mel tento endpoint brat jako seed pro interpretaci vkusu:

- vybrat reprezentativni oblibene tituly,
- precist lokalni rating a slovni plus/minus poznamky,
- pouzit zanry, people affinity, title role signals a genre score signaly jako
  kontext,
- navrhy vracet pozdeji oddelene jako externi doporuceni, ne jako prepis lokalniho scoringu.

### Role seznamu pro AI tipy

Kazdy fyzicky seznam ma v databazi `ai_input_role`. V UI se pro to pozdeji
pouzije cesky popisek `Pro AI tipy`.

Povolene hodnoty:

- `strong_positive`: silne pozitivni priklady, typicky `Kouknout znou`.
- `interested_owned`: slabsi pozitivni signal, titul je stazeny nebo s nim byla
  rucni prace, typicky `Mam`.
- `interested_planned`: zajem videt, typicky `Watchlist`, `Koukni rychle`,
  `Stahnout`.
- `in_progress`: rozkoukane nebo rozehrane, opatrny signal zajmu.
- `negative`: negativni priklady nebo veci, ktere nechceme brat jako podobne
  pozitivni tipy.
- `external_suggestion`: vystup z AI, napr. seznam `AI navrhy`; nikdy ho
  nepouzivat jako vstup pro AI tipy.
- `ignore`: technicke nebo neutralni seznamy, napr. `Plex Library`.

### Soucasna omezeni

- `people_affinity` vraci konkretni hodnocene osoby z kreditu titulu. Agregovane
  `actor_affinity_rating` zustava samostatny souhrnny signal z hlavniho obsazeni.
- `title_role_signals` vraci titulove vazane signaly role/postavy. Je to
  samostatna vrstva pro pripady, kdy celkovy rating titulu neni vysoky, ale
  konkretni postava, dialogy, chovani, vzhled nebo pritazlivost jsou dulezity
  pozitivni nebo negativni signal.
- Endpoint zatim vybira priklady ze seznamu. Planuje se rozsireni nebo samostatne
  endpointy podle `ai_input_role`, pro tituly s lokalnim ratingem vyssim nez
  zadany prah a pro tituly, ktere obsahuji slovni hodnoceni.
- Endpoint nema samostatnou autentizaci; pocita se s lokalnim pouzitim nebo
  provozem za existujicim lokalnim/Tailscale omezenim.

## GET `/api/ai/rated-titles`

Stav: implementovano.

Read-only endpoint pro predani titulů s lokalnim hodnocenim od zadaneho prahu.
Ma slouzit jako cilenejsi seed nez seznamovy `taste-seed`, napriklad pro dotaz
"ukaz filmy, ktere Jiri hodnotil 8 a vic".

### Query parametry

- `min_user_rating`: minimum lokalniho ratingu vcetne, minimum `1`, maximum
  `10`, vychozi `8`.
- `limit`: pocet vracenych titulu, minimum `1`, maximum `200`, vychozi `50`.
- `title_type`: volitelne zúzeni na `movie`, `tvSeries`, `tvMiniSeries` a podobne.

### Priklad volani

```bash
curl 'http://127.0.0.1:8019/api/ai/rated-titles?min_user_rating=8&limit=20'
```

### Poznamka ke kontraktu

Payload polozky ma byt kompatibilni s `taste-seed` tam, kde to dava smysl:
IMDb/TMDB identita, nazev, rok, zanry, IMDb rating, Jiriho rating, slovni
poznamky, `people_affinity`, `title_role_signals` a `genre_score_signals`.

### Struktura odpovedi

```json
{
  "filters": {
    "min_user_rating": 8,
    "title_type": null
  },
  "limit": 20,
  "items": [
    {
      "imdb_id": "tt0133093",
      "tconst": "tt0133093",
      "tmdb_id": 603,
      "title": "The Matrix",
      "original_title": "The Matrix",
      "title_type": "movie",
      "year": 1999,
      "genres": ["Action", "Sci-Fi"],
      "imdb_rating": 8.7,
      "imdb_votes": 2100000,
      "user_rating": 9,
      "liked_notes": "Co fungovalo.",
      "disliked_notes": null,
      "rated_at": "2026-07-19T18:20:00",
      "actor_affinity_rating": 9.0,
      "people_affinity": [],
      "title_role_signals": [],
      "genre_score_signals": []
    }
  ]
}
```

## GET `/api/ai/taste-inputs`

Stav: implementovano.

Read-only endpoint, ktery vraci vstupy pro AI podle databazove role seznamu
`ai_input_role`. Na rozdil od `/api/ai/taste-seed` nebere jeden konkretni
seznam, ale projde vsechny viditelne seznamy a rozdeli je podle vyznamu pro AI.

### Ucel

- dat externimu AI projektu sirsi obraz vkusu nez jeden seed seznam,
- rozlisit silne pozitivni priklady, slabsi zajem, rozkoukane a negativni
  priklady,
- nikdy neposlat zpet jako vstup seznamy s roli `external_suggestion` nebo
  `ignore`,
- zachovat stejny tvar polozek jako v `/api/ai/taste-seed`.

### Query parametry

- `limit_per_list`: pocet polozek z kazdeho seznamu, minimum `1`, maximum `100`,
  vychozi `25`.

### Priklad volani

```bash
curl 'http://127.0.0.1:8019/api/ai/taste-inputs?limit_per_list=10'
```

### Struktura odpovedi

```json
{
  "contract_version": 1,
  "limit_per_list": 10,
  "included_roles": [
    "strong_positive",
    "interested_owned",
    "interested_planned",
    "in_progress",
    "negative"
  ],
  "excluded_roles": ["external_suggestion", "ignore"],
  "role_descriptions": {
    "strong_positive": "Silne pozitivni priklady.",
    "interested_owned": "Tituly, ktere Jiri ma nebo s nimi udelal rucni praci.",
    "interested_planned": "Tituly, ktere chce videt nebo stoji za pozornost.",
    "in_progress": "Rozkoukane tituly; opatrny signal zajmu.",
    "negative": "Negativni priklady nebo veci, ktere nemaji podporovat podobne pozitivni tipy.",
    "external_suggestion": "Vystup z AI; nepouzivat jako vstup.",
    "ignore": "Neutralni nebo technicke seznamy; nepouzivat jako vstup."
  },
  "groups": {
    "strong_positive": [
      {
        "source_list": {
          "id": "custom-list-...",
          "slug": "kouknout-znou",
          "name": "Kouknout znou",
          "ai_input_role": "strong_positive"
        },
        "role_description": "Silne pozitivni priklady.",
        "limit": 10,
        "items": []
      }
    ],
    "interested_owned": [],
    "interested_planned": [],
    "in_progress": [],
    "negative": []
  },
  "excluded_sources": [
    {
      "id": "ai-suggestions",
      "slug": "ai-navrhy",
      "name": "AI navrhy",
      "ai_input_role": "external_suggestion",
      "item_count": 0
    }
  ],
  "usage_notes": [
    "`external_suggestion` a `ignore` se nikdy neposilaji jako vstup pro AI tipy.",
    "`negative` je vstup pro varovani a vymezovani vkusu, ne pozitivni seed.",
    "Polozky ve skupinach maji stejny tvar jako `/api/ai/taste-seed` vcetne people affinity a title role signals."
  ]
}
```

### Poznamka ke kontraktu

`negative` neni pozitivni doporucovaci seed. Externi AI projekt ho ma brat jako
vymezeni vkusu: co neopakovat, cemu se vyhnout, pripadne proc podobny titul
nedoporucit bez dobreho vysvetleni.

## GET `/api/ai/noted-titles`

Stav: planovano.

Read-only endpoint pro tituly, ktere maji vyplnene slovni hodnoceni
`liked_notes` nebo `disliked_notes`. Ma byt prakticky dulezity pro ChatGPT,
protoze slovni hodnoceni obsahuje nejvetsi hustotu osobniho vkusu.

### Navrzene query parametry

- `notes`: `any`, `liked`, nebo `disliked`; vychozi `any`.
- `min_user_rating`: volitelny filtr podle lokalniho ratingu.
- `limit`: pocet vracenych titulu, minimum `1`, maximum `200`.

### Poznamka ke kontraktu

Payload polozky ma byt kompatibilni s `taste-seed`, ale endpoint ma radit
prednostne podle existence a cerstvosti slovnich poznamek, ne podle seznamu.
