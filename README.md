# nrp-docs-watch

Sleduje změny dokumentace v [`NRP-CZ/docs`](https://github.com/NRP-CZ/docs)
(adresář `content/`) a jednou za hodinu o nich píše anglicky formulovaná
shrnutí jako komentáře do denních digest issues v tomto repozitáři. Shrnutí
generuje LLM na e-INFRA (`llm.ai.e-infra.cz`) z commit message a diffu; ke
každé změně přidává přímé odkazy na zasažené stránky publikované dokumentace
<https://nrp-cz.github.io/docs/>.

## Jak to funguje

1. GitHub Actions workflow (`.github/workflows/watch.yml`) běží každou hodinu
   (nebo ručně přes *Run workflow*).
2. `watch_docs.py` se přes GitHub API zeptá na commity v `content/` od poslední
   kontroly (stav je v `state.json`, commituje se zpět do repa).
3. Pro každý nový commit, který mění `.md`/`.mdx` soubory, zavolá LLM na
   e-INFRA a nechá si vygenerovat krátké české shrnutí pro čtenáře dokumentace.
4. Shrnutí + odkazy na publikované stránky přibydou jako komentář v digest
   issue daného dne.

**Denní digesty:** pro každý den, ve kterém došlo ke změně, vzniká jedno issue
(„NRP-CZ/docs documentation changes – YYYY-MM-DD") a všechny commity toho dne
do něj přibývají jako komentáře. Den bez změn = žádné issue. Seznam digestů:
[Issues](../../issues?q=label%3Adocs-digest).

**Notifikace:** u digest issue klikni na **Subscribe** → GitHub ti pošle mail
při každém novém komentáři. Nebo sleduj celé repo (Watch → Custom → Issues).

## Nastavení

1. V repozitáři: **Settings → Secrets and variables → Actions → New repository
   secret** → `E_INFRA_API_TOKEN` = tvůj token pro `llm.ai.e-infra.cz`.
2. Volitelně v záložce **Variables** nastav `E_INFRA_MODEL` (výchozí `kimi-k3`).
3. První běh workflow vytvoří digest issue; od dalších běhů přibývají komentáře.

## Konfigurace (env proměnné)

| Proměnná | Výchozí | Popis |
|---|---|---|
| `WATCH_REPO` | `NRP-CZ/docs` | sledovaný repozitář |
| `WATCH_BRANCH` | `main` | sledovaná větev |
| `WATCH_PATH` | `content` | sledovaný adresář |
| `E_INFRA_MODEL` | `kimi-k3` | model na llm.ai.e-infra.cz |

## Lokální běh

```bash
export GH_TOKEN=$(gh auth token)
export E_INFRA_API_TOKEN=…
python watch_docs.py --init   # jednorázově: zapíše aktuální HEAD, nic nehlásí
python watch_docs.py          # normální běh
```

Bez závislostí — jen Python 3.10+ standardní knihovna.

## Backfill (zpracování historie)

Chceš zpracovat starší commity najednou? V záložce **Actions → Watch NRP-CZ/docs
→ Run workflow** vyplň `since` (např. `2026-08-08`) a `max_commits` (např.
`50`). Skript ignoruje uložený stav a zpracuje všechny commity od zadaného
data. Lokálně totéž přes env proměnné:

```bash
SINCE_DATE=2026-08-08 MAX_COMMITS_PER_RUN=50 python watch_docs.py
```
