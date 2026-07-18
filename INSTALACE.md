# Instalace projektu na jinem pocitaci

Kratky postup pro novy stroj nebo druhou kopii projektu.

## Predpoklady

- macOS nebo Linux
- Python 3.14+
- `uv`
- Git
- PostgreSQL, pokud ma bezet aktualni PG runtime s realnymi daty

## Prvni instalace

```bash
git clone https://github.com/kulda69/FILMY.git
cd FILMY
uv sync
```

Pokud uz je repozitar na stroji stazeny:

```bash
cd /cesta/k/FILMY
git pull --ff-only
uv sync
```

## Lokalni konfigurace

V rootu projektu vytvor `.env`. Hesla a tokeny nepatri do gitu.

Minimalni PostgreSQL runtime potrebuje hlavne:

```text
POSTGRES_APP_HOST=/private/tmp
POSTGRES_APP_PORT=5432
POSTGRES_APP_DATABASE=filmy
POSTGRES_APP_USER=filmy_app
POSTGRES_APP_PASSWORD=...
```

Volitelne pro TMDB enrichment:

```text
TMDB_API_READ_ACCESS_TOKEN=...
```

Administratorske PostgreSQL promenne jsou potreba jen pro bootstrap, migrace
nebo obnovu databaze; bezne spusteni appky je nema potrebovat.

## Data

Git obsahuje kod a dokumentaci, ne lokalni databaze, assety ani exporty. Na
novem pocitaci je potreba dodat data jednou z cest:

- obnovit PostgreSQL databazi `filmy` ze zalohy,
- nebo znovu spustit importni/bootstrap postupy,
- a pripadne prenest lokalni assety z `data/assets/`, pokud jsou potreba hned.

## Kontrola a spusteni

```bash
.venv/bin/pytest
.venv/bin/python main.py
```

Aplikace bezi na:

```text
http://127.0.0.1:8019
```

## Automaticke spusteni na Mac mini

V repozitari je pripraveny LaunchAgent:

```text
deploy/cz.kulda.filmy.plist
```

Je nastaveny na cestu:

```text
/Volumes/kulda/apps/FILMY
```

Pokud bude projekt na Mac mini jinde, uprav v plist hodnoty
`ProgramArguments` a `WorkingDirectory`.

Instalace LaunchAgentu:

```bash
mkdir -p logs
cp deploy/cz.kulda.filmy.plist ~/Library/LaunchAgents/cz.kulda.filmy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cz.kulda.filmy.plist
launchctl enable gui/$(id -u)/cz.kulda.filmy
launchctl kickstart -k gui/$(id -u)/cz.kulda.filmy
```

Kontrola:

```bash
launchctl print gui/$(id -u)/cz.kulda.filmy
tail -n 100 logs/launchd.stderr.log
```

Odinstalace:

```bash
launchctl bootout gui/$(id -u)/cz.kulda.filmy
rm ~/Library/LaunchAgents/cz.kulda.filmy.plist
```

## Rychla aktualizace pozdeji

```bash
cd /cesta/k/FILMY
git pull --ff-only
uv sync
.venv/bin/pytest
.venv/bin/python main.py
```
