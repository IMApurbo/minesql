# minesql

A MySQL terminal that only suggests the **next** SQL piece.

* Gray "ghost" text after your cursor = the current best suggestion
* `Tab` — accept the suggestion
* `Shift+Tab` — show the next-best suggestion
* `Ctrl+T` — toggle NEXT-piece mode ↔ FULL inline mode (whole query predicted)
* `Ctrl+O` — suggestions ON/OFF (OFF is the default at startup)
* `Ctrl+Space` — LIST mode ON/OFF: a live suggestion dropdown that stays open
  and updates as you type until you toggle it off (works even when ghost
  suggestions are OFF). `Alt+L` is an alias if `Ctrl+Space` is grabbed by your
  terminal/OS.
* `Alt+/` — force ONE suggestion right here, right now (any cursor position)
* FULL mode: `Tab` accepts ONE word, `Ctrl+E` (or `Ctrl+Tab`, if your terminal
  sends it) accepts the WHOLE suggestion
* `Enter` — run the query
* `Ctrl+D` — exit

Suggestions are schema-aware: database, table, and column names (plus sample
values) come straight from the live MySQL server via `information_schema`. A
small n-gram model learns from the queries you run (saved in
`~/.minesql_model.json`) and re-ranks suggestions so your habits float to
the top.

## Install

```bash
pip install minesql
```

## Usage

```bash
minesql -H 127.0.0.1 -u root -d mydb
```

```
usage: minesql [-h] [-H HOST] [-P PORT] [-u USER] [-p PASSWORD] [-d DATABASE]
                [--suggest] [--full] [--no-confirm] [--no-values]

MySQL terminal with schema-aware ghost-text SQL suggestions.

options:
  -h, --help            show this help message and exit
  -H HOST, --host HOST
  -P PORT, --port PORT
  -u USER, --user USER
  -p PASSWORD, --password PASSWORD
  -d DATABASE, --database DATABASE
                        database to start in
  --suggest             start with suggestions ON (default is OFF)
  --full                start with FULL inline suggestions ON
  --no-confirm          don't ask before DELETE/UPDATE without WHERE, DROP, TRUNCATE
  --no-values           don't sample column values for suggestions (faster on huge tables)
```

### Examples

```bash
minesql -u root -d shop
minesql -H db.example.com -P 3307 -u app --full
```

### Keys (full reference)

| Key | Action |
|---|---|
| `Tab` | accept the gray suggestion (in FULL mode: just the next word/unit) |
| `Ctrl+E` | accept the WHOLE suggestion (also `Ctrl+Tab` on terminals that send it, e.g. kitty, WezTerm, iTerm2, xterm with modifyOtherKeys) |
| `Shift+Tab` | cycle to the next-best suggestion |
| `Ctrl+O` | suggestions ON/OFF (OFF by default; start ON with `--suggest` or `--full`) |
| `Ctrl+Space` / `Alt+L` | LIST mode ON/OFF — a live dropdown of suggestions (with type labels: keyword/table/column/value/snippet) that stays open and updates as you type until toggled off. Works even while ghost suggestions are OFF. `Tab`/`Shift+Tab` move in the list, `Enter` picks the highlighted item (it does **not** run the query). |
| `Alt+/` | force ONE suggestion at the cursor right now (ghost at end of line, dropdown mid-line), even while OFF |
| `Ctrl+T` | switch suggestion mode NEXT ↔ FULL (also turns suggestions on) |
| Right arrow | accept without the trailing space |
| Up/Down | command history |
| `Enter` | run query |
| `Ctrl+C` | cancel line |
| `Ctrl+D` | exit |

### In-app commands

```
\help      show the in-app help
\full      toggle FULL/NEXT mode (turns suggestions on)
\suggest   toggle suggestions ON/OFF
\list      toggle LIST mode
\q / exit / quit   leave
```

### Safety

`DELETE`/`UPDATE` without `WHERE`, `DROP`, and `TRUNCATE` ask for confirmation
first (disable with `--no-confirm`).

### Modes

* **NEXT** — suggests only the next keyword / table / column / value.
* **FULL** — chains predictions into a whole query. Optional clauses
  (`WHERE`, `AND`, `ORDER BY`, `LIMIT`, ...) are only added once the model has
  learned you use them after that context. Sample values are guesses — edit
  them.

### Suggestion sources

Live schema (`SHOW DATABASES`, `information_schema`), sample column values,
and an n-gram model learned from your queries (`~/.minesql_model.json`).

## Requirements

* Python 3.8+
* [`prompt_toolkit`](https://pypi.org/project/prompt-toolkit/)
* [`PyMySQL`](https://pypi.org/project/PyMySQL/)

These are installed automatically with `pip install minesql`.

## Development

```bash
git clone https://github.com/IMApurbo/minesql.git
cd minesql
pip install -e .
minesql --help
```

## License

MIT © [IMApurbo](https://github.com/IMApurbo)
