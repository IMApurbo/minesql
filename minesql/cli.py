#!/usr/bin/env python3
"""
minesql - a MySQL terminal that only suggests the NEXT SQL piece.

  * Gray "ghost" text after your cursor = the current best suggestion
  * Tab        -> accept the suggestion
  * Shift+Tab  -> show the next-best suggestion
  * Ctrl+T     -> toggle NEXT-piece mode <-> FULL inline mode (whole query predicted)
  * Ctrl+O     -> suggestions ON/OFF (OFF is the default at startup)
  * Ctrl+Space -> LIST mode ON/OFF: a live suggestion dropdown that stays open and
                  updates as you type until you toggle it off (works even when ghost
                  suggestions are OFF). Alt+L is an alias if Ctrl+Space is grabbed.
  * Alt+/      -> force ONE suggestion right here, right now (any cursor position)
  * FULL mode: Tab accepts ONE word, Ctrl+E (or Ctrl+Tab, if your terminal sends it)
    accepts the WHOLE suggestion
  * Enter      -> run the query
  * Ctrl+D     -> exit

Suggestions are schema-aware: database, table, column names (and sample values)
come from the live MySQL server via information_schema. A small n-gram model
learns from the queries you run (saved in ~/.minesql_model.json) and re-ranks
suggestions so your habits float to the top.

Install:  pip install minesql
Run:      minesql -H 127.0.0.1 -u root -d mydb
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pymysql
from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

MODEL_PATH = Path.home() / ".minesql_model.json"
HISTORY_PATH = Path.home() / ".minesql_history"

KEYWORDS = set("""
SELECT FROM WHERE AND OR NOT IN LIKE BETWEEN IS NULL JOIN LEFT RIGHT INNER OUTER CROSS ON
GROUP BY ORDER HAVING LIMIT OFFSET AS DISTINCT INSERT INTO VALUES VALUE UPDATE SET DELETE
CREATE DROP ALTER TABLE DATABASE SCHEMA USE SHOW DESCRIBE DESC ASC EXPLAIN TRUNCATE UNION
COUNT SUM AVG MIN MAX EXISTS ADD COLUMN MODIFY CHANGE INDEX RENAME TO VIEW
""".split())

START = ["SELECT", "SHOW", "INSERT INTO", "UPDATE", "DELETE FROM", "USE", "DESCRIBE",
         "CREATE TABLE", "CREATE DATABASE", "DROP TABLE", "ALTER TABLE", "EXPLAIN",
         "TRUNCATE TABLE"]
ONE = {"DELETE": ["FROM"], "INSERT": ["INTO"], "CREATE": ["TABLE", "DATABASE", "INDEX", "VIEW"],
       "DROP": ["TABLE", "DATABASE", "VIEW", "INDEX"], "ALTER": ["TABLE"], "TRUNCATE": ["TABLE"]}
OPS = {"=", "<", ">", "<=", ">=", "!=", "<>", "<=>", "LIKE", "BETWEEN"}
OPERATORS = ["=", ">", "<", ">=", "<=", "!=", "LIKE", "IN (", "BETWEEN", "IS NULL", "IS NOT NULL"]
HELP = """
KEYS
  Tab          accept the gray suggestion (in FULL mode: just the next word/unit)
  Ctrl+E       accept the WHOLE suggestion (also Ctrl+Tab on terminals that send it,
               e.g. kitty, WezTerm, iTerm2, xterm with modifyOtherKeys)
  Shift+Tab    cycle to the next-best suggestion
  Ctrl+O       suggestions ON / OFF   (OFF by default; start ON with --suggest or --full)
  Ctrl+Space   LIST mode ON/OFF - a live dropdown of suggestions (with type labels:
               keyword/table/column/value/snippet) that stays open and updates as you
               type until toggled off. Works even while ghost suggestions are OFF.
               Tab/Shift+Tab move in the list, Enter picks the highlighted item
               (it does NOT run the query). Alt+L is an alias for Ctrl+Space.
  Alt+/        force ONE suggestion at the cursor right now (ghost at end of line,
               dropdown mid-line), even while OFF.
  Ctrl+T       switch suggestion mode NEXT <-> FULL (also turns suggestions on)
  Right arrow  accept without the trailing space
  Up/Down      command history
  Enter        run query      Ctrl+C cancel line      Ctrl+D exit

IN-APP COMMANDS
  \\help   show this help        \\full   toggle FULL/NEXT mode (turns suggestions on)
  \\suggest   toggle suggestions ON/OFF      \\list   toggle LIST mode

SAFETY
  DELETE/UPDATE without WHERE, DROP and TRUNCATE ask for confirmation first
  (disable with --no-confirm).
  \\q / exit / quit   leave

MODES
  NEXT  suggests only the next keyword / table / column / value.
  FULL  chains predictions into a whole query. Optional clauses (WHERE, AND,
        ORDER BY, LIMIT ...) are only added once the model has learned you use
        them after that context. Sample values are guesses - edit them.

SUGGESTION SOURCES
  Live schema (SHOW DATABASES, information_schema), sample column values, and an
  n-gram model learned from your queries (~/.minesql_model.json).
"""
TYPES = ["INT", "VARCHAR(255)", "VARCHAR(100)", "TEXT", "BIGINT", "DECIMAL(10,2)", "DATE",
         "DATETIME", "TIMESTAMP", "BOOLEAN", "TINYINT"]
COL_TEMPLATES = ["id INT AUTO_INCREMENT PRIMARY KEY", "name VARCHAR(100) NOT NULL",
                 "email VARCHAR(255) UNIQUE", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                 "status TINYINT DEFAULT 1", "price DECIMAL(10,2)", "description TEXT"]
COL_CONSTRAINTS = ("NOT", "PRIMARY", "UNIQUE", "DEFAULT", "AUTO_INCREMENT")
OPTIONAL = {"ENGINE=INNODB", "NOT", "PRIMARY", "AUTO_INCREMENT", "UNIQUE", "DEFAULT", "WHERE", "JOIN", "LEFT", "INNER", "RIGHT", "GROUP", "ORDER", "LIMIT", "AND", "OR",
            "AS", "HAVING", "OFFSET", "DISTINCT"}
NUMERIC = {"int", "bigint", "smallint", "tinyint", "mediumint", "decimal", "float", "double", "year"}

TOK = re.compile(
    r"'(?:[^'\\]|\\.)*'?|\"(?:[^\"\\]|\\.)*\"?|[\w$]+(?:\.[\w$*]*)*|<=>|<=|>=|<>|!=|[=<>(),;*+\-/%]"
)


def kw(xs):
    return [(x, True) for x in xs]


def nm(xs):
    return [(x, False) for x in xs]


def norm(t):
    if t[0] in "'\"":
        return "<str>"
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return "<num>"
    return t.upper() if t.upper() in KEYWORDS else t.lower()


# --------------------------------------------------------------------------- schema
class Schema:
    """Live view of the MySQL server: databases -> tables -> (column, type)."""

    def __init__(self, conn):
        self.conn = conn
        self.db = None
        self.databases = []
        self.cache = {}
        self.value_cache = {}
        self.refresh()

    def q(self, sql, args=None):
        with self.conn.cursor() as c:
            c.execute(sql, args)
            return c.fetchall()

    def refresh(self):
        self.db = self.q("SELECT DATABASE()")[0][0]
        self.databases = [r[0] for r in self.q("SHOW DATABASES")]
        self.cache.clear()
        self.value_cache.clear()

    def tbls(self, db=None):
        db = db or self.db
        if not db:
            return {}
        if db not in self.cache:
            try:
                t = defaultdict(list)
                for tn, cn, dt in self.q(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME, ORDINAL_POSITION", (db,)):
                    t[tn].append((cn, str(dt).lower()))
                self.cache[db] = dict(t)
            except Exception:
                self.cache[db] = {}
        return self.cache[db]

    def find_db(self, name):
        return next((d for d in self.databases if d.lower() == name.lower()), None)

    def find_table(self, name, db=None):
        return next((t for t in self.tbls(db) if t.lower() == name.lower()), None)


# --------------------------------------------------------------------------- engine
class Engine:
    """Context-aware next-token suggester + tiny learned n-gram ranker."""

    def __init__(self, schema, sample=True):
        self.s = schema
        self.sample = sample
        try:
            self.model = json.loads(MODEL_PATH.read_text())
        except Exception:
            self.model = {"uni": {}, "bi": {}, "tri": {}}

    # ---- learning
    def learn(self, sql):
        toks = ["^"] + [norm(t) for t in TOK.findall(sql.replace("`", ""))]
        m = self.model
        for i in range(1, len(toks)):
            for table, key in (("uni", toks[i]),
                               ("bi", f"{toks[i-1]}|{toks[i]}"),
                               ("tri", f"{toks[i-2]}|{toks[i-1]}|{toks[i]}" if i >= 2 else None)):
                if key:
                    m[table][key] = m[table].get(key, 0) + 1
        try:
            MODEL_PATH.write_text(json.dumps(m))
        except Exception:
            pass

    def score(self, cand, p1, p2):
        k = norm(cand.split()[0])
        m = self.model
        return (3 * m["tri"].get(f"{p2}|{p1}|{k}", 0) + 2 * m["bi"].get(f"{p1}|{k}", 0)
                + 0.2 * m["uni"].get(k, 0))

    # ---- schema helpers
    def query_tables(self, toks):
        U = [t.upper() for t in toks]
        out = []
        for j, t in enumerate(U):
            if t in ("FROM", "JOIN", "INTO") or (t == "UPDATE" and j == 0):
                if j + 1 >= len(toks):
                    continue
                name, db = toks[j + 1], None
                if "." in name:
                    dbn, name = name.split(".", 1)
                    db = self.s.find_db(dbn)
                real = self.s.find_table(name, db)
                if not real:
                    continue
                alias = real
                if j + 2 < len(toks):
                    if U[j + 2] == "AS" and j + 3 < len(toks):
                        alias = toks[j + 3]
                    elif U[j + 2] not in KEYWORDS and re.match(r"[A-Za-z_]", toks[j + 2]):
                        alias = toks[j + 2]
                out.append((alias, db or self.s.db, real))
        return out

    def select_columns(self, toks):
        """Plain column names already written in the SELECT list (before the trailing FROM)."""
        U = [t.upper() for t in toks]
        if "SELECT" not in U:
            return []
        body = toks[len(U) - U[::-1].index("SELECT"): -1]
        cols = []
        for k, t in enumerate(body):
            if not re.fullmatch(r"[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*)?", t) or t.upper() in KEYWORDS:
                continue
            if (k + 1 < len(body) and body[k + 1] == "(") or (k and body[k - 1].upper() == "AS"):
                continue  # function name or alias
            cols.append(t.rpartition(".")[2].lower())
        return cols

    # ---- CREATE TABLE / CREATE DATABASE templates
    @staticmethod
    def demo_name(existing, base):
        ex = {x.lower() for x in existing}
        n = 1
        while f"{base}_{n}" in ex:
            n += 1
        return f"{base}_{n}"

    def create_bodies(self, existing):
        out = [("(id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)", True),
               ("(id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, "
                "email VARCHAR(255) UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)", True)]
        for t in existing:                      # variant linked to a real table via foreign key
            if any(c.lower() == "id" for c, _ in self.s.tbls()[t]):
                out.append((f"(id INT AUTO_INCREMENT PRIMARY KEY, {t.lower()}_id INT NOT NULL, "
                            f"created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                            f"FOREIGN KEY ({t.lower()}_id) REFERENCES {t}(id))", False))
                break
        return out

    @staticmethod
    def create_state(rest):
        """Walk tokens after the table name -> (tokens of current column def, paren depth, column names)."""
        depth, seg, names, started = 0, [], [], False
        for t in rest:
            if t == "(":
                depth += 1
                if depth == 1:
                    seg, started = [], True
                    continue
            elif t == ")":
                depth -= 1
                if depth == 0:
                    continue
            elif t == "," and depth == 1:
                seg = []
                continue
            if started:
                if (not seg and depth == 1 and re.match(r"[A-Za-z_]", t)
                        and t.upper() not in ("PRIMARY", "FOREIGN", "UNIQUE", "KEY", "INDEX", "CONSTRAINT")):
                    names.append(t)
                seg.append(t)
        return seg, depth, names

    def create_table(self, since):
        S = [t.upper() for t in since]
        existing = list(self.s.tbls())
        had_if = False
        if S[:3] == ["IF", "NOT", "EXISTS"]:
            since, S, had_if = since[3:], S[3:], True
        elif S[:1] == ["IF"]:
            return kw(["NOT EXISTS"] if len(S) == 1 else ["EXISTS"] if len(S) == 2 else [])
        bodies = self.create_bodies(existing)
        if not S:                               # demo name + complete structure
            name = self.demo_name(existing, "table")
            out = [(f"{name} {b}", k) for b, k in bodies]
            if not had_if:
                out.append((f"IF NOT EXISTS {name} {bodies[0][0]}", True))
            return out
        if len(S) == 1:
            return bodies
        seg, depth, names = self.create_state(since[1:])
        if depth <= 0:
            return kw(["ENGINE=InnoDB"]) if S[-1] == ")" else []
        Sg = [t.upper() for t in seg]
        inner = Sg.count("(") - Sg.count(")")
        if Sg and Sg[0] in ("PRIMARY", "FOREIGN", "UNIQUE"):   # table-level constraint
            if Sg[-1] in ("PRIMARY", "FOREIGN", "UNIQUE") and len(Sg) == 1:
                return kw(["KEY ("])
            if Sg[-1] == "KEY":
                return kw(["("])
            if inner > 0:
                return nm(names) if Sg[-1] in ("(", ",") else kw([")"])
            if Sg[0] == "FOREIGN":
                if "REFERENCES" not in Sg:
                    return kw(["REFERENCES"])
                ri = Sg.index("REFERENCES")
                if len(Sg) == ri + 1:
                    return nm(existing)
                if len(Sg) == ri + 2:
                    t = self.s.find_table(seg[ri + 1])
                    return nm([f"({self.s.tbls()[t][0][0]})"]) if t else []
            return kw([")"])
        if not Sg:
            used = {n.lower() for n in names}
            cols = [c for c in COL_TEMPLATES if c.split()[0] not in used]
            extra = ["PRIMARY KEY (", "FOREIGN KEY ("] if names else []
            return kw(cols + extra)
        if inner > 0:
            return kw(["255)", "100)", "50)", "10,2)"]) if Sg[-1] == "(" else kw([")"])
        if Sg[-1] == "PRIMARY":
            return kw(["KEY"])
        if Sg[-1] == "NOT":
            return kw(["NULL"])
        if Sg[-1] == "DEFAULT":
            return kw(["NULL", "0", "1", "CURRENT_TIMESTAMP"])
        if len(Sg) == 1:                        # column name typed -> guess a sensible type
            n, types = seg[0].lower(), list(TYPES)
            pref = ("INT" if n == "id" or n.endswith("_id") else
                    "TIMESTAMP" if n.endswith("_at") else
                    "DATE" if n.endswith("date") else
                    "DECIMAL(10,2)" if n in ("price", "amount", "salary", "total") else
                    "TEXT" if n in ("description", "notes", "content", "body") else None)
            if pref:
                types.remove(pref)
                types.insert(0, pref)
            return kw(types)
        cons = [c for c in ("NOT NULL", "PRIMARY KEY", "AUTO_INCREMENT", "UNIQUE", "DEFAULT")
                if c.split()[0] not in Sg]
        has = any(w in Sg for w in COL_CONSTRAINTS)
        return kw([")"] + cons) if has else kw(cons + [")"])

    def columns(self, qt, qualified):
        out = []
        if qt:
            for alias, db, t in qt:
                for c, _ in self.s.tbls(db).get(t, []):
                    out.append(f"{alias}.{c}" if qualified else c)
        else:
            for cols in self.s.tbls().values():
                out += [c for c, _ in cols]
        return list(dict.fromkeys(out))

    def resolve(self, tok, qt):
        q, _, c = tok.rpartition(".")
        for alias, db, t in qt:
            if q and q.lower() not in (alias.lower(), t.lower()):
                continue
            for col, typ in self.s.tbls(db).get(t, []):
                if col.lower() == c.lower():
                    return db, t, col, typ
        return None

    def values(self, qt, tok):
        if not self.sample:
            return []
        r = self.resolve(tok, qt)
        if not r:
            return []
        db, t, col, typ = r
        key = (db, t, col)
        if key not in self.s.value_cache:
            try:
                rows = self.s.q(f"SELECT DISTINCT `{col}` FROM `{db}`.`{t}` "
                                f"WHERE `{col}` IS NOT NULL LIMIT 5")
            except Exception:
                rows = []
            self.s.value_cache[key] = [x[0] for x in rows if not isinstance(x[0], (bytes, bytearray))]
        out = []
        for v in self.s.value_cache[key]:
            s = str(v)
            if len(s) <= 30:
                out.append(s if typ in NUMERIC else "'" + s.replace("'", "''") + "'")
        return out

    # ---- grammar: what can come next?
    def expect(self, toks, partial):
        if "." in partial:
            q = partial.rpartition(".")[0]
            for alias, db, t in self.query_tables(toks):
                if q.lower() in (alias.lower(), t.lower()):
                    return nm([f"{q}.{c}" for c, _ in self.s.tbls(db).get(t, [])])
            d = self.s.find_db(q)
            if d:
                return nm([f"{q}.{t}" for t in self.s.tbls(d)])
            t = self.s.find_table(q)
            return nm([f"{q}.{c}" for c, _ in self.s.tbls()[t]]) if t else []

        U = [t.upper() for t in toks]
        if U[:1] == ["EXPLAIN"]:
            toks, U = toks[1:], U[1:]
            if not U:
                return kw(["SELECT", "UPDATE", "DELETE FROM", "INSERT INTO"])
        if not U:
            return kw(START)
        first, last = U[0], U[-1]
        if len(U) == 1 and first in ONE:
            return kw(ONE[first])
        if last in ("LEFT", "RIGHT"):
            return kw(["JOIN", "OUTER JOIN"])
        if last in ("INNER", "CROSS", "OUTER"):
            return kw(["JOIN"])
        if last in ("GROUP", "ORDER"):
            return kw(["BY"])

        clause = head = copen = byname = None
        since = []
        for j, t in enumerate(U):
            if first == "CREATE" and clause == "table":
                since.append(toks[j])
                continue
            if t == "SELECT":
                clause, since = "select", []
            elif (t in ("FROM", "JOIN", "INTO") or (t in ("UPDATE", "DESCRIBE", "DESC") and j == 0)
                  or (t == "TABLE" and j > 0 and U[j - 1] in ("CREATE", "DROP", "ALTER", "TRUNCATE"))):
                clause, head, since = "table", t, []
            elif t in ("DATABASE", "SCHEMA", "USE"):
                clause, since = "db", []
            elif t == "SHOW" and j == 0:
                clause, since = "show", []
            elif t in ("WHERE", "HAVING", "ON"):
                clause, copen, since = "cond", t, []
            elif t in ("AND", "OR") and clause == "cond":
                since = []
            elif t == "BY":
                clause, byname, since = "by", (U[j - 1] if j else ""), []
            elif t == "LIMIT":
                clause, since = "limit", []
            elif t == "SET" and j > 0:
                clause, since = "set", []
            elif t in ("VALUES", "VALUE"):
                clause, since = "values", []
            else:
                since.append(toks[j])

        S = [s.upper() for s in since]
        qt = self.query_tables(toks)
        qual = len(qt) > 1
        depth = since.count("(") - since.count(")")
        is_col = lambda tok: self.resolve(tok, qt) is not None
        cols = lambda: nm(self.columns(qt, qual))
        tables = lambda: nm(list(self.s.tbls())) + (nm([d + "." for d in self.s.databases]) if partial else [])

        if clause == "db":
            if first == "CREATE":
                return nm([self.demo_name(self.s.databases, "db")]) if not S else []
            return [] if S else nm(self.s.databases)

        if clause == "show":
            if not S:
                return kw(["DATABASES", "TABLES", "COLUMNS FROM", "CREATE TABLE", "INDEX FROM",
                           "PROCESSLIST", "STATUS", "VARIABLES", "TABLE STATUS"])
            return kw(["TABLE"]) if S[-1] == "CREATE" else []

        if clause == "table" and first == "CREATE":
            return self.create_table(since)

        if clause == "table":
            if not S:
                if head == "FROM" and first == "SELECT":   # only tables that contain the chosen columns
                    need = set(self.select_columns(toks))
                    ok = [t for t, cl in self.s.tbls().items() if need <= {c.lower() for c, _ in cl}]
                    if need and ok:
                        return nm(ok) + (nm([d + "." for d in self.s.databases]) if partial else [])
                return tables()
            if first in ("DESCRIBE", "DESC", "TRUNCATE", "DROP", "CREATE"):
                return []
            if S[-1] == ",":
                return tables()
            if S[-1] == "AS":
                return []
            if first == "INSERT":
                t = self.s.find_table(since[0].split(".")[-1])
                tc = [c for c, _ in self.s.tbls().get(t, [])] if t else []
                if "(" not in S:
                    return nm(["(" + ", ".join(tc) + ") VALUES ("]) if tc else kw(["("])
                if ")" in S:
                    return kw(["VALUES"])
                return nm(tc) if S[-1] in ("(", ",") else kw([")"])
            if head == "JOIN":
                return kw(["ON"])
            if first in ("SELECT", "DELETE"):
                return kw(["WHERE", "JOIN", "LEFT JOIN", "INNER JOIN", "RIGHT JOIN",
                           "GROUP BY", "ORDER BY", "LIMIT"])
            if first == "UPDATE":
                return kw(["SET"])
            if first == "ALTER":
                if len(S) == 1:
                    return kw(["ADD COLUMN", "DROP COLUMN", "MODIFY COLUMN", "ADD INDEX", "RENAME TO"])
                if S[-1] == "COLUMN" and len(S) >= 2 and S[-2] in ("DROP", "MODIFY", "CHANGE"):
                    t = self.s.find_table(since[0])
                    return nm([c for c, _ in self.s.tbls().get(t, [])]) if t else []
            return []

        if clause == "select":
            funcs = ["COUNT(*)", "COUNT(", "MAX(", "MIN(", "SUM(", "AVG("]
            if not S:
                return kw(["*", "DISTINCT"]) + cols() + kw(funcs)
            if S[-1] in (",", "DISTINCT"):
                return cols() + kw(funcs)
            if S[-1] == "(":
                return kw(["*"]) + cols()
            if S[-1] == "*" and len(S) >= 2 and S[-2] == "(":
                return kw([")"])
            if depth > 0:
                return kw([")"])
            return [] if S[-1] == "AS" else kw(["FROM", "AS"])

        if clause == "cond":
            if not S or S[-1] in ("AND", "OR"):
                return cols()
            if S[-1] == "(":
                if len(S) >= 3 and S[-2] == "IN":
                    return nm(self.values(qt, since[-3]))
                return cols()
            if S[-1] == "NOT":
                return kw(["LIKE", "IN", "BETWEEN"]) if len(S) > 1 and is_col(since[-2]) else cols()
            if S[-1] == "IS":
                return kw(["NULL", "NOT NULL"])
            if S[-1] == "IN":
                return kw(["("])
            if S[-1] in OPS and len(S) >= 2:
                return nm(self.values(qt, since[-2]))
            if is_col(since[-1]):
                return kw(OPERATORS)
            if depth > 0:
                return kw([")"])
            if copen == "ON":
                return kw(["WHERE", "JOIN", "LEFT JOIN", "AND", "GROUP BY", "ORDER BY", "LIMIT"])
            if copen == "WHERE" and first == "SELECT":
                return kw(["AND", "OR", "GROUP BY", "ORDER BY", "LIMIT"])
            return kw(["AND", "OR", "ORDER BY", "LIMIT"])

        if clause == "by":
            if not S or S[-1] == ",":
                return cols()
            if S[-1] in ("ASC", "DESC"):
                return kw(["LIMIT"])
            return kw(["HAVING", "ORDER BY", "LIMIT"]) if byname == "GROUP" else kw(["DESC", "ASC", "LIMIT"])

        if clause == "limit":
            return kw(["10", "100", "1"]) if not S else (kw(["OFFSET"]) if len(S) == 1 else [])

        if clause == "set":
            if not S or S[-1] == ",":
                return nm(self.columns(qt, False))
            if S[-1] == "=":
                return nm(self.values(qt, since[-2])) if len(S) >= 2 else []
            return kw(["="]) if is_col(since[-1]) else kw(["WHERE"])

        if clause == "values":
            return kw(["("]) if not S else []

        return []

    # ---- public: list of ghost-text suffixes, best first
    def suggestions(self, text):
        stmt = text.rsplit(";", 1)[-1].replace("`", "")
        toks = TOK.findall(stmt)
        if toks and toks[-1][0] in "'\"" and (len(toks[-1]) < 2 or toks[-1][-1] != toks[-1][0]):
            return []  # inside an unfinished string literal
        partial = ""
        if toks and not stmt[-1].isspace() and re.match(r"[\w$]", toks[-1]):
            partial = toks.pop()
        lead = "" if (not text or text[-1].isspace() or text[-1] == "(") else " "

        def pick(c, p):
            return [(x, k) for x, k in c if x.lower().startswith(p.lower()) and x.lower() != p.lower()]

        try:
            picked = pick(self.expect(toks, partial), partial)
            if partial and not picked:          # word is already complete -> suggest what follows it
                toks.append(partial)
                partial, lead = "", " "
                picked = pick(self.expect(toks, ""), "")
        except Exception:
            return []

        lower = partial.islower() if partial else any(t.islower() for t in toks if t.upper() in KEYWORDS)
        p1 = norm(toks[-1]) if toks else "^"
        p2 = norm(toks[-2]) if len(toks) > 1 else "^"
        ranked = sorted(enumerate(picked), key=lambda p: (-self.score(p[1][0], p1, p2), p[0]))
        out = []
        for _, (c, is_kw) in ranked:
            if is_kw and lower:
                c = c.lower()
            s = c[len(partial):] if partial else lead + c
            if s not in out:
                out.append(s)
        return out[:30]

    # ---- FULL mode: chain best suggestions into a whole query
    def chain(self, text, first=None, max_steps=12):
        out, cur, s = "", text, first
        for step in range(max_steps):
            if s is None:
                opts = self.suggestions(cur)
                if not opts:
                    break
                s = opts[0]
                word = s.strip().split()[0].upper() if s.strip() else ""
                if word in OPTIONAL:            # only add optional clauses you've used before
                    toks = TOK.findall(cur.rsplit(";", 1)[-1].replace("`", ""))
                    p1 = norm(toks[-1]) if toks else "^"
                    p2 = norm(toks[-2]) if len(toks) > 1 else "^"
                    if self.score(word, p1, p2) <= 0:
                        break
            if s.startswith(")") and out.endswith(" "):
                out, cur = out[:-1], cur[:-1]
            piece = s + ("" if s.endswith(("(", ".")) else " ")
            out += piece
            cur += piece
            if s.endswith("("):                 # needs user input here
                break
            s = None
        return out.rstrip()


# --------------------------------------------------------------------------- prompt_toolkit glue
class GhostSuggest(AutoSuggest):
    def __init__(self, engine):
        self.engine, self.idx, self.text, self.options = engine, 0, None, []
        self.full = False
        self.enabled = False      # suggestions are OFF by default
        self.list_mode = False    # persistent dropdown list (Ctrl+Space)
        self.forced = None        # text for which the user forced a one-off suggestion

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor
        if not self.enabled and self.forced != text:
            self.options = []
            return None
        if text != self.text:
            self.text, self.idx = text, 0
            self.options = self.compute(text)
        if not self.options:
            return None
        return Suggestion(self.options[self.idx % len(self.options)])

    def compute(self, text):
        opts = self.engine.suggestions(text)
        if not self.full:
            return opts
        out = []
        for o in opts[:8]:
            f = self.engine.chain(text, first=o)
            if f and f not in out:
                out.append(f)
        return out

    def toggle_enabled(self, buffer):
        self.enabled = not self.enabled
        self.text = self.forced = None
        buffer.suggestion = self.get_suggestion(buffer, buffer.document)

    def force(self, buffer):
        """Suggest right now at the cursor, regardless of ON/OFF. Returns True if ghost text shown."""
        doc = buffer.document
        if doc.is_cursor_at_the_end and not (buffer.suggestion and buffer.suggestion.text):
            self.forced, self.text = doc.text_before_cursor, None
            buffer.suggestion = self.get_suggestion(buffer, doc)
            return buffer.suggestion is not None
        return False

    def toggle(self, buffer):
        self.full = not self.full
        self.enabled = True
        self.text = None
        buffer.suggestion = self.get_suggestion(buffer, buffer.document)

    def next(self, buffer):
        if self.options:
            self.idx = (self.idx + 1) % len(self.options)
        buffer.suggestion = self.get_suggestion(buffer, buffer.document)

    def reset(self):
        self.text = self.forced = None


class GhostCompleter(Completer):
    """Live dropdown (LIST mode / force key); honours NEXT/FULL mode and labels each item."""

    def __init__(self, ghost):
        self.ghost = ghost

    def kind(self, item):
        w, sc = item.strip(), self.ghost.engine.s
        if not w:
            return ""
        if w[0] in "'\"" or re.fullmatch(r"\d+(\.\d+)?", w):
            return "value"
        f = re.split(r"[\s(]", w)[0]
        if f.upper() in KEYWORDS:
            return "keyword"
        g = f.rpartition(".")[2].lower()
        if g in {t.lower() for t in sc.tbls()}:
            return "table"
        if g in {c.lower() for cl in sc.tbls().values() for c, _ in cl}:
            return "column"
        if g in {d.lower() for d in sc.databases}:
            return "database"
        return "snippet" if (len(w) > 25 or not f) else ""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        m = re.search(r"[\w$.]+$", text)
        partial = m.group(0) if m else ""
        for o in self.ghost.compute(text)[:15]:
            shown = (partial + o) if partial else (o.strip() or o)
            yield Completion(o, start_position=0, display=shown, display_meta=self.kind(shown))


def first_word(s):
    """First unit of a suggestion: a word, a quoted value, or a (...) group; keeps leading space."""
    rest = s.lstrip()
    if not rest:
        return s
    lead = s[:len(s) - len(rest)]
    if rest[0] in "'\"" and rest.find(rest[0], 1) > 0:
        end = rest.find(rest[0], 1) + 1
    elif rest[0] == "(" and ")" in rest:
        d, end = 0, len(rest)
        for i, ch in enumerate(rest):
            d += (ch == "(") - (ch == ")")
            if d == 0:
                end = i + 1
                break
    else:
        end = re.match(r"\S+", rest).end()
    return lead + rest[:end]


def risky(stmt):
    u = re.sub(r"\s+", " ", stmt.strip().upper())
    if re.match(r"(DROP|TRUNCATE)\b", u):
        return "this permanently removes data/structure"
    if re.match(r"(DELETE|UPDATE)\b", u) and not re.search(r"\bWHERE\b", u):
        return "no WHERE clause - this affects ALL rows"
    return None


def show(cur):
    if not cur.description:
        print(f"OK, {cur.rowcount} row(s) affected")
        return
    head = [d[0] for d in cur.description]
    rows = [["NULL" if v is None else str(v)[:40] for v in r] for r in cur.fetchmany(200)]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(head)]
    line = "+" + "+".join("-" * (x + 2) for x in w) + "+"
    fmt = lambda r: "| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |"
    print(line, fmt(head), line, *[fmt(r) for r in rows], line, sep="\n")
    print(f"{len(rows)} row(s)")


def main():
    ap = argparse.ArgumentParser(
        prog="minesql", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="MySQL terminal with schema-aware ghost-text SQL suggestions.",
        epilog=HELP + "\nEXAMPLES\n  minesql -u root -d shop\n"
                      "  minesql -H db.example.com -P 3307 -u app --full\n")
    ap.add_argument("-H", "--host", default="127.0.0.1")
    ap.add_argument("-P", "--port", type=int, default=3306)
    ap.add_argument("-u", "--user", default="root")
    ap.add_argument("-p", "--password", default=os.environ.get("MYSQL_PWD"))
    ap.add_argument("-d", "--database", help="database to start in")
    ap.add_argument("--suggest", action="store_true", help="start with suggestions ON (default is OFF)")
    ap.add_argument("--full", action="store_true", help="start with FULL inline suggestions ON")
    ap.add_argument("--no-confirm", dest="confirm", action="store_false",
                    help="don't ask before DELETE/UPDATE without WHERE, DROP, TRUNCATE")
    ap.add_argument("--no-values", action="store_true",
                    help="don't sample column values for suggestions (faster on huge tables)")
    a = ap.parse_args()
    if a.password is None:
        a.password = getpass.getpass("Password: ")
    try:
        conn = pymysql.connect(host=a.host, port=a.port, user=a.user, password=a.password,
                               database=a.database, autocommit=True, charset="utf8mb4")
    except pymysql.MySQLError as e:
        sys.exit(f"Could not connect: {e}")

    schema = Schema(conn)
    engine = Engine(schema, sample=not a.no_values)
    ghost = GhostSuggest(engine)
    ghost.full = a.full
    ghost.enabled = a.full or a.suggest

    kb = KeyBindings()

    def insert(b, t):
        if t.startswith(")") and b.text.endswith(" "):
            b.delete_before_cursor(1)
        b.insert_text(t + ("" if t.endswith(("(", ".")) else " "))

    @kb.add("tab")
    def _accept(event):                      # FULL mode: one word at a time
        b = event.current_buffer
        if b.complete_state:                 # dropdown open: move through it
            b.complete_next()
        elif b.suggestion and b.suggestion.text:
            t = b.suggestion.text
            insert(b, first_word(t) if ghost.full else t)

    @kb.add("c-o")
    def _onoff(event):
        ghost.toggle_enabled(event.current_buffer)
        event.app.invalidate()

    @kb.add("escape", "/")
    def _force(event):                       # one-off suggestion at the cursor
        b = event.current_buffer
        if not ghost.force(b):
            b.start_completion(select_first=False)
        event.app.invalidate()

    @kb.add("c-space")
    @kb.add("escape", "l")
    def _list(event):                        # persistent LIST mode toggle
        b = event.current_buffer
        ghost.list_mode = not ghost.list_mode
        if ghost.list_mode:
            b.start_completion(select_first=False)
        else:
            b.complete_state = None
        event.app.invalidate()

    picked = Condition(lambda: bool(get_app().current_buffer.complete_state
                                    and get_app().current_buffer.complete_state.current_completion))

    @kb.add("enter", filter=picked)
    def _pick(event):                        # Enter on a highlighted item = choose it, not run
        b = event.current_buffer
        b.complete_state = None
        if not b.text.endswith((" ", "(", ".")):
            b.insert_text(" ")

    @kb.add("c-e")
    @kb.add("f20")                           # f20 = Ctrl+Tab on terminals that report it
    def _accept_all(event):
        b = event.current_buffer
        if b.suggestion and b.suggestion.text and b.document.is_cursor_at_the_end:
            insert(b, b.suggestion.text)
        else:
            b.cursor_position = len(b.text)  # normal Ctrl+E: jump to end of line

    try:                                     # teach prompt_toolkit common Ctrl+Tab sequences
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys
        for seq in ("\x1b[27;5;9~", "\x1b[9;5u", "\x1b[1;5I"):
            ANSI_SEQUENCES[seq] = Keys.F20
    except Exception:
        pass

    @kb.add("c-t")
    def _mode(event):
        ghost.toggle(event.current_buffer)
        event.app.invalidate()

    @kb.add("s-tab")
    def _next(event):
        if event.current_buffer.complete_state:
            event.current_buffer.complete_previous()
            return
        ghost.next(event.current_buffer)
        event.app.invalidate()

    def toolbar():
        n = len(ghost.options)
        pos = f" ({ghost.idx % n + 1}/{n})" if n else ""
        mode = "OFF" if not ghost.enabled else ("FULL" if ghost.full else "NEXT")
        if ghost.list_mode:
            mode += "+LIST"
        keys = ("Ctrl+O on" if not ghost.enabled else
                "Tab 1 word | Ctrl+E all" if ghost.full else "Tab accept")
        return (f" [{mode}] {keys} | Ctrl+Space list | Alt+/ force | Shift+Tab next{pos} "
                f"| Ctrl+T mode | \\help")

    style = Style.from_dict({"auto-suggestion": "fg:#808080", "bottom-toolbar": "bg:#222222 #aaaaaa"})
    session = PromptSession(history=FileHistory(str(HISTORY_PATH)), auto_suggest=ghost,
                            key_bindings=kb, style=style, completer=GhostCompleter(ghost),
                            complete_while_typing=Condition(lambda: ghost.list_mode), bottom_toolbar=toolbar,
                            enable_history_search=False)

    def prime():  # show a suggestion even on an empty prompt
        b = get_app().current_buffer
        b.suggestion = ghost.get_suggestion(b, b.document)

    print(f"Connected to {a.host}:{a.port} as {a.user}. Current database: {schema.db}")
    print("Suggestions " + ("ON" if ghost.enabled else "OFF (Ctrl+O to enable)")
          + " | Ctrl+Space list on/off | Alt+/ force one | Tab accept | Ctrl+T FULL/NEXT | \\help")
    while True:
        ghost.reset()
        try:
            sql = session.prompt(lambda: f"mysql [{schema.db or '-'}]> ", pre_run=prime).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not sql:
            continue
        if sql.lower() in ("exit", "quit", "\\q"):
            break
        if sql.lower() == "\\help":
            print(HELP)
            continue
        if sql.lower() == "\\full":
            ghost.full, ghost.enabled = not ghost.full, True
            print("Mode:", "FULL" if ghost.full else "NEXT")
            continue
        if sql.lower() == "\\list":
            ghost.list_mode = not ghost.list_mode
            print("List mode:", "ON" if ghost.list_mode else "OFF")
            continue
        if sql.lower() == "\\suggest":
            ghost.enabled = not ghost.enabled
            print("Suggestions:", "ON" if ghost.enabled else "OFF")
            continue
        ran = True
        try:
            with conn.cursor() as cur:
                t0 = time.time()
                for stmt in filter(None, (s.strip() for s in sql.split(";"))):
                    why = risky(stmt) if a.confirm else None
                    if why and input(f"\033[33mWARNING: {why}. Run anyway? [y/N] \033[0m"
                                     ).strip().lower() not in ("y", "yes"):
                        print("Cancelled.")
                        ran = False
                        break
                    cur.execute(stmt)
                    show(cur)
                if ran:
                    print(f"({time.time() - t0:.3f}s)")
            if ran:
                engine.learn(sql)
                if sql.split()[0].upper() in ("USE", "CREATE", "DROP", "ALTER", "RENAME", "TRUNCATE"):
                    schema.refresh()
        except pymysql.MySQLError as e:
            print(f"\033[31mERROR: {e}\033[0m")
        except (KeyboardInterrupt, EOFError):
            print("Cancelled.")
    conn.close()


if __name__ == "__main__":
    main()
