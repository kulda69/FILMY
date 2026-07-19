Projekt: Filmy a seriály

Oslovení a styl

- Uživatel se jmenuje Jiří (Kulda)
- Odpovídat stručně, nevymýšlet si, občas vtipná poznámka, jinak solidní a uzemněný tón.
- Komunikace v češtině.

Technické prostředí

- Mac s Apple Silicon.
- Python, správa prostředí přes uv.
- Stack: FastAPI + Jinja2 + Bootstrap 5.3 (jen vestavěné utility třídy, data-bs-theme="dark", bez vlastního CSS).
- Bez Reactu a složitých abstrakcí — jednoduchá, čitelná řešení.
- Lokálně může běžet Ollama (modely nomic-embed-text pro embeddingy, qwen3:14b pro generování).
- Appka se spouští jako desktopová přes Brave/Chrome app mód, případně později pywebview.
- DuckDB větve už neaktualizovat o nové funkce ani nové schéma. Cílový směr je PostgreSQL-only; zbylý DuckDB kód pouze inventarizovat, držet bez dalšího rozvoje nebo plánovaně odstranit.
- `API_ENDPOINTY.md` je živý kontrakt pro navazující projekty. Při přidání, změně nebo upřesnění veřejného/navazujícího endpointu ho průběžně aktualizovat.
- `filmy_output/` je stabilní lokální zdroj AI doporučení pro import zpět do FILMY. Importer má číst jeho standardní JSON schéma; prázdná/nepoužitá pole zůstávají jako `null` nebo prázdné seznamy, ne jako chybějící pole.

Pracovní styl

- Před většími změnami mini plán.
- Postupovat v malých inkrementálních krocích, na konci každého krátké shrnutí.
- Kvalitní docstringy, čitelnost před chytrostí.

Cíl projektu

Osobní webová appka pro filmy a seriály — co jsem viděl, co si chci vybrat, a kde je to v ČR dostupné.

Datové zdroje (vše zdarma)

- IMDb TSV dumpy (datasets.imdbws.com) jako základní katalog — stažené lokálně, čtené přímo v DuckDB. Při importu filtrovat (jen filmy/seriály, ne epizody jako samostatné řádky, případně od určitého roku), protože title.basics má ~11M záznamů.
- TMDB API (zdarma pro osobní použití) — plakáty, popisy, česká metadata. Propojení přes IMDb ID (external_ids).
- Dostupnost v ČR — TMDB watch providers endpoint (data od JustWatch). Pozor: cache na API až ~8h, občas nekompletní. Nutná atribuce JustWatch.

Historie sledování

- Vlastní tabulka v DuckDB.
  - Jednorázový import z Netflix CSV exportu (Účet → Viewing Activity → Download all) a z exportu Traktu.
- Trakt.tv se vyřazuje (placený, nespolehlivá synchronizace) — appka má historii vlastnit lokálně.
- Po dokoukání značit "viděno" přímo v appce.

Doporučování ("co by se mi mohlo líbit")

- Jednoduchá verze (start): filtrování a řazení katalogu podle žánrů, hodnocení, roku a ručně nastavených preferencí, odvozeno z historie + hodnocení. Čistý DuckDB dotaz.
- Chytřejší verze (později): embeddingy popisů přes Ollama (nomic-embed-text), vektorové vyhledávání přes DuckDB rozšíření vss (není nutná ChromaDB), qwen3:14b pro slovní zdůvodnění doporučení.

Další stav

- Zatím nezačato. Příští krok: návrh schématu databáze a struktury projektu.

---

Poznámka: API klíče (IMDb/TMDB) nepatří sem do instrukcí — dej je do .env v kódu projektu.

<!-- codex-project-brain:start -->
## Project Brain

- This project uses the `project-brain` skill for durable memory and recall.
- At the start of substantive work, read `PLAN.md` and recall relevant `memories.sh` context.
- Record important events in `Historie projektu.md` and reasons or rejected approaches in `Rozhodnuti projektu.md`.
- Propagate durable AI rules into `AGENTS.md`; keep current-stage direction and the next action in `PLAN.md`.
- Use the stable `project_id` from `.agents/project-brain.json` for structured memories.
- Ask `Mam to zapsat?` when long-term importance or the correct destination is unclear.
<!-- codex-project-brain:end -->
