Projekt: Filmy a seriály

## Oslovení a styl

- Uživatel se jmenuje Jiří (Kulda)
- v kazde opovedi ho oslovuj Jiří nebo Kuldo
- Odpovídej stručně, věcně a v češtině s diakritikou, i když uživatel píše bez ní.
- Nevymýšlej si neověřená fakta.

## Technické prostředí

- Mac s Apple Silicon.
- Python, správa prostředí přes uv.
- Stack: FastAPI + Jinja2 + Bootstrap 5.3 (jen vestavěné utility třídy, data-bs-theme="dark", bez vlastního CSS).
- Bez Reactu a složitých abstrakcí — jednoduchá, čitelná řešení.
- Appka se spouští jako desktopová přes Brave/Chrome app mód, případně později pywebview.
- Projekt je PostgreSQL-only. Nezavádět zpět souborový databázový backend ani fallback větve mimo PostgreSQL.
- Po nasazení na server už se databáze nepřenáší ručně mezi stroji. Každá změna PostgreSQL schématu, funkce, view, constraintu, indexu nebo seed/role dat musí mít idempotentní upgrade krok spustitelný přes `filmy-upgrade-database` / `python -m filmy.scripts.upgrade_database`; upgrade runner má sám ověřit stav přes tabulku verzí a provést jen chybějící kroky.
- `API_ENDPOINTY.md` je živý kontrakt pro navazující projekty. Při přidání, změně nebo upřesnění veřejného/navazujícího endpointu ho průběžně aktualizovat.
- `filmy_output/` je stabilní lokální zdroj AI doporučení pro import zpět do FILMY. Importer má číst jeho standardní JSON schéma; prázdná/nepoužitá pole zůstávají jako `null` nebo prázdné seznamy, ne jako chybějící pole.
- API klíče (IMDb/TMDB) nepatří sem do instrukcí; patří do `.env` v kódu projektu.

## Zdroj pravdy

- Přednost mají aktuální soubory v projektu a ověřené lokální chování.
- Pokud existují `AGENTS.md`, `PLAN.md`, handoff nebo jiné řídicí poznámky přímo v projektu, ber je přednostně.
- Pokud je projekt teprve v ideové fázi, používej `PREPLAN.md` jako zdroj počáteční orientace, dokud nevznikne skutečný `PLAN.md`.
- Nezaměňuj návrh, domněnku nebo starou poznámku za ověřený stav.

## Pracovní styl

- Před většími změnami mini plán.
- Postupovat v malých inkrementálních krocích, na konci každého krátké shrnutí.
- Kdyz se do `PLAN.md` zapisuje navrh, how-to nebo navazujici postup v samostatnem souboru, pridat k tomu primo odkaz na ten konkretni soubor; pokud existuje technicky navrh i lidsky/manualovy vyklad, odkazat oba.
- Kvalitní docstringy, čitelnost před chytrostí. docstringy podle běžných Python pravidel; v česky orientovaném projektu je piš česky bez háčků a čárek.
- Kde v Pythonu dava smysl skutecna `class` jako nosic vicekrokove logiky nebo sdileneho kontextu, vytvor ji i bez explicitniho vyzadani od Jiriho. Nepouzivat ji jen jako formalni obal bez prinosu.
- Kdyz uz Python `class` vznika, dopln podrobny docstring tridy i jejich dulezitych metod. Docstringy drzet prakticke, konkretni a psane cesky bez hacku a carek.
- Piš Python kód tak, aby ho bylo možné i po AI zásahu dál ručně číst a upravovat.
- Pokud projekt používá databázi, nepřesouvej do Pythonu logiku, kterou databáze umí vyřešit přirozeně, efektivně a čitelně sama.
- Nenechávej v jednom přerostlém souboru více nesouvisejících odpovědností jen proto, že to zatím funguje.
- Pokud se v projektu opakovaně potvrdí stejná provozní překážka a existuje ověřený workaround, ber ji jako pracovní pravidlo a neopakuj znovu stejný slepý pokus.

## Cíl projektu

Osobní webová appka pro filmy a seriály — co jsem viděl, co si chci vybrat, a kde je to v ČR dostupné.

## Datové zdroje (vše zdarma)

- IMDb TSV dumpy (datasets.imdbws.com) jako základní katalog — stažené lokálně a importované do PostgreSQL katalogových tabulek. Při importu filtrovat (jen filmy/seriály, ne epizody jako samostatné řádky, případně od určitého roku), protože title.basics má ~11M záznamů.
- TMDB API (zdarma pro osobní použití) — plakáty, popisy, česká metadata. Propojení přes IMDb ID (external_ids).
- Dostupnost v ČR — TMDB watch providers endpoint (data od JustWatch). Pozor: cache na API až ~8h, občas nekompletní. Nutná atribuce JustWatch.

## Historie sledování

- Vlastní tabulky v PostgreSQL.
- Jednorázový import z Netflix CSV exportu (Účet → Viewing Activity → Download all) a z exportu Traktu.
- Trakt.tv se vyřazuje (placený, nespolehlivá synchronizace) — appka má historii vlastnit lokálně.
- Po dokoukání značit "viděno" přímo v appce.

<!-- codex-project-brain:start -->
## Project Brain

- This project uses the `project-brain` skill for durable memory and recall.
- V tomto projektu používat výhradně novou verzi `project-brain` jako continuity vrstvu; staré session logy nebo jiné historické paměťové vrstvy brát jen jako nouzový fallback, když nestačí lokální project-brain soubory.
- At the start of substantive work, read `PLAN.md` and recall relevant `memories.sh` context.
- Record important events in `Historie projektu.md` and reasons or rejected approaches in `Rozhodnuti projektu.md`.
- Propagate durable AI rules into `AGENTS.md`; keep current-stage direction and the next action in `PLAN.md`.
- Use the stable `project_id` from `.agents/project-brain.json` for structured memories.
- Ask `Mam to zapsat?` when long-term importance or the correct destination is unclear.
<!-- codex-project-brain:end -->
