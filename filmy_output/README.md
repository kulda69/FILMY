# FILMY output

Tento adresar slouzi pro vystupni JSON doporuceni vytvorena v tomto projektu nad vstupy z `../filmy_input/`.

## Ucel

- `FILMY` aplikace zustava zdrojem dat a interniho scoringu.
- Tento projekt pridava interpretaci, porovnani signalu, rizika a vysvetlene tipy.
- Posledni faze prace ma ulozit doporuceni jako JSON sem, aby slo dal zpracovat nebo importovat zpet do aplikace.

## Pravidla

- Standardni vystupni format je az do odvolani maximalni stabilni schema: vsechna znama pole jsou pritomna vzdy a nepouzita pole maji `null` nebo prazdny seznam.
- Vystup nesmi prepisovat lokalni scoring aplikace; `FILMY` score je jen jeden ze vstupnich signalu.
- Kazde doporuceni ma mit jasny duvod, uroven jistoty a rizika.
- Negativni a rozkoukane vstupy pouzivat jako vymezovaci kontext, ne jako absolutni zakaz.
- `external_suggestion` / `AI navrhy` se nesmi pouzit jako vstup pro dalsi doporuceni.
- Pri online overovani dostupnosti nebo aktualnich informaci zapsat zdroj a datum overeni.
- Vystupni JSON neskladat rucne, pokud to jde skriptem. Pouzij `../scripts/build_recommendations.py`.
- Pred importem nebo napojenim aplikace over strukturu pres `../scripts/validate_output_schema.py`.
- Import ve `FILMY` ma cist maximalni stabilni schema. Pole, ktera se pro dany workflow nehodi, maji byt pritomna jako `null` nebo prazdny seznam, ne chybet.

## Opakovatelna tvorba vystupu

AI ma pripravit strukturovany draft s doporucovacim usudkem. Skript doplni metadata, seradi podle priority, zvaliduje povinna pole a ulozi stabilni JSON:

```bash
python3 scripts/build_recommendations.py path/to/draft.json filmy_output/recommendations-YYYY-MM-DD-slug.json
```

## Navrzeny tvar souboru

```json
{
  "contract_version": 1,
  "created_at": "2026-07-19T13:00:00+02:00",
  "intent": "recommendation_batch",
  "status": "draft_for_review",
  "source_inputs": [
    "../filmy_input/context.json",
    "../filmy_input/scoring-explainer.json",
    "../filmy_input/taste-inputs-limit-3.json"
  ],
  "method_notes": [],
  "deprioritized_candidates": [],
  "notes": null,
  "recommendations": [
    {
      "title": "Example",
      "year": 2020,
      "imdb_id": "tt0000000",
      "tmdb_id": null,
      "media_type": "movie",
      "confidence": "medium",
      "fit_reasons": [
        "Proc to sedi podle pozitivnich vstupu."
      ],
      "risk_reasons": [
        "Co muze vadit podle negativnich nebo slabich signalu."
      ],
      "source_signal_refs": [
        {
          "source": "taste-inputs",
          "role": "strong_positive",
          "source_list": "Kouknout znovu",
          "title": "Dog Tags",
          "imdb_id": "tt1212408",
          "selection_score": null,
          "watch_state": null,
          "source_markers": [],
          "genres": [],
          "tmdb_id": null,
          "year": null,
          "media_type": null,
          "notes": null
        }
      ],
      "status": "candidate",
      "notes": null
    }
  ]
}
```
