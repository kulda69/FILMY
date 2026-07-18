# API endpointy pro navazujici projekty

Tento soubor popisuje stabilizovane nebo pripravovane endpointy, ktere maji
pouzivat jine lokalni projekty. Neni to dump vsech internich admin rout.

Pri zmene verejneho/navazujiciho endpointu aktualizovat tento soubor ve stejnem
rezu jako kod. Projekt `/Volumes/not_inserted/AI/filmy-knihy` ho muze brat jako
kontrakt.

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
    "people_affinity": "Konkretni osoby z titulu, ktere maji rucni affinity rating; kontrakt pro navazujici rozsireni taste-seed."
  },
  "usage_notes": [
    "Endpoint je read-only a nevola externi AI ani online katalogy.",
    "Navazujici AI projekt ho ma volat jako obecny kontext pred praci s konkretnimi tituly.",
    "Favorite genres a favorite traits se vraci cele, vcetne neaktivnich polozek."
  ]
}
```

## GET `/api/ai/scoring-explainer`

Stav: planovano.

Read-only endpoint pro vysvetleni, jak FILMY pocita lokalni score a jak ma
navazujici AI projekt cist jednotlive signaly. Je to zakladni kontextovy
endpoint podobny `/api/ai/context`, ale nebude vracet preference ani tituly;
bude vracet metodiku.

### Ucel

- vysvetlit rozdil mezi lokalnim scoringem, IMDb ratingem a Jiriho ratingem,
- popsat, z ceho vznikaji `genre_score_signals`,
- vysvetlit vyznam `watch_signal_score`, `rating_signal_score`,
  `actor_affinity_score` a `final_score`,
- popsat, jak se do score zapocitavaji `Favorite Genres` a pozdeji
  `Favorite Traits`,
- dat ChatGPT/navazujicimu projektu textovy navod, jak signaly interpretovat,
  ne jen syrova cisla.

### Navrzena struktura odpovedi

```json
{
  "contract_version": 1,
  "score_scope": "default",
  "principles": [
    "Lokalni score je pomocny signal pro razeni a vysvetleni, ne definitivni pravda.",
    "Jiriho lokalni rating ma vyssi vyznam nez externi IMDb rating.",
    "Favorite Genres a Favorite Traits jsou rucne zadane preference; preference_rank 1 je silnejsi nez 10."
  ],
  "signals": {
    "final_score": "Normalizovane lokalni score zanru nebo kandidata.",
    "watch_signal_score": "Signal odvozeny z historie sledovani.",
    "rating_signal_score": "Signal odvozeny z Jiriho lokalnich ratingu.",
    "actor_affinity_score": "Signal odvozeny z oblibenosti osob navazanych na titul.",
    "favorite_genres": "Rucni zanrove preference, ktere score podporuji.",
    "favorite_traits": "Rucni trait preference; zatim sbirane, pozdeji vstoupi do vysvetleni a score."
  },
  "limitations": [
    "Skore nevysvetluje samo o sobe kvalitu filmu.",
    "Bez slovniho hodnoceni je interpretace vkusu mene presna.",
    "People affinity musi byt ctena jako osobni signal, ne jako obecna popularita herce."
  ]
}
```

## GET `/api/ai/taste-seed`

Stav: implementovano, payload se bude jeste rozsirovat.

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
    "list_kind": "custom"
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
- pouzit zanry, people affinity a genre score signaly jako kontext,
- navrhy vracet pozdeji oddelene jako externi doporuceni, ne jako prepis lokalniho scoringu.

### Soucasna omezeni

- `people_affinity` je pozadovany kontrakt, ale aktualni implementace zatim vraci
  jen agregovane `actor_affinity_rating`. Dalsi rez ma doplnit konkretni hodnocene
  osoby u kazdeho titulu.
- Endpoint zatim vybira priklady ze seznamu. Planuje se rozsireni nebo samostatne
  endpointy pro tituly s lokalnim ratingem vyssim nez zadany prah a pro tituly,
  ktere obsahuji slovni hodnoceni.
- Endpoint nema samostatnou autentizaci; pocita se s lokalnim pouzitim nebo
  provozem za existujicim lokalnim/Tailscale omezenim.

## GET `/api/ai/rated-titles`

Stav: planovano.

Read-only endpoint pro predani titulů s lokalnim hodnocenim od zadaneho prahu.
Ma slouzit jako cilenejsi seed nez seznamovy `taste-seed`, napriklad pro dotaz
"ukaz filmy, ktere Jiri hodnotil 8 a vic".

### Navrzene query parametry

- `min_user_rating`: minimum lokalniho ratingu vcetne, vychozi pravdepodobne `8`.
- `limit`: pocet vracenych titulu, minimum `1`, maximum `200`.
- `title_type`: volitelne zúzeni na `movie`, `tvSeries`, `tvMiniSeries` a podobne.

### Poznamka ke kontraktu

Payload polozky ma byt kompatibilni s `taste-seed` tam, kde to dava smysl:
IMDb/TMDB identita, nazev, rok, zanry, IMDb rating, Jiriho rating, slovni
poznamky, `people_affinity` a `genre_score_signals`.

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
