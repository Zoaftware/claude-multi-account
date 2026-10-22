"""Roles: an account can be limited to one stage of a task (planning, design, development or audit).

A role is kept in accounts.json ("role", set with `ma.py roles`) or given for one run only (`split --roles`). A task
then goes through its stages in order - plan -> design -> dev -> audit - and each stage runs on its own accounts:
  - an account with a role only takes pieces of that stage;
  - an account with no role ("general") takes any stage, but only when no account with that stage's role can work;
  - a stage that no account may ever take stops the run before it starts.
Join pieces and audit fixes are development.
"""
import unicodedata

import split as S

STAGES = ["plan", "design", "dev", "audit"]
NAME = {"plan": "planning", "design": "design", "dev": "development", "audit": "audit"}
VERB = {"plan": "plan", "design": "design", "dev": "develop", "audit": "audit"}
ONLY = {"plan": "only plans", "design": "only designs", "dev": "only develops", "audit": "only audits"}
ALIASES = {}
for _stage, _words in {
        "plan": "plan plans planning planner planificar planifica planificacion planear",
        "design": "design designs designer diseno disenar disena",
        "dev": "dev develop develops development developer code coding desarrollo desarrollar desarrolla programar",
        "audit": "audit audits auditor qa review reviewer auditar audita auditoria revisar"}.items():
    for _w in _words.split():
        ALIASES[_w] = _stage
GENERAL = {"general", "any", "all", "none", "everything", "todo", "todas", "ninguno", "nada", "libre", "normal"}


def _plain(text):
    t = unicodedata.normalize("NFD", str(text or "").strip().lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def normal(text):
    """'audit' -> 'audit', 'Planning' -> 'plan', 'general' -> None; anything else is an error."""
    t = _plain(text)
    if t in GENERAL:
        return None
    if t in ALIASES:
        return ALIASES[t]
    raise ValueError("unknown role '%s': use plan, design, dev or audit (or general to take the role away)" % text)


def parse(parts):
    """['work=audit', 'team=dev'] or 'work=audit,team=dev' -> {name: stage | None}."""
    if isinstance(parts, str):
        parts = [parts]
    out = {}
    for part in parts or []:
        for pair in part.replace(",", " ").split():
            if "=" not in pair:
                raise ValueError("each role goes as account=role, for example work=audit (got '%s')" % pair)
            name, role = pair.split("=", 1)
            out[name.strip()] = normal(role)
    return out


def stages_of(text):
    """'plan,dev,audit' -> the stages, in task order."""
    asked = set()
    for word in str(text or "").replace(",", " ").split():
        s = ALIASES.get(_plain(word))
        if s is None:
            raise ValueError("'%s' is not a stage: use plan, design, dev or audit" % word)
        asked.add(s)
    return [s for s in STAGES if s in asked]


def of_accounts(lst):
    """The roles saved in accounts.json: {name: stage} for the accounts that have one."""
    return {c["name"]: c["role"] for c in lst["accounts"] if c.get("role") in STAGES}


def stage_of(p):
    return p.get("stage") or "dev"


def fit(roles, stage, rows, exclude=()):
    """The rows that may take a piece of this stage now: the accounts with this stage's role if one of them is
    ready, else the accounts with no role. An account with another role never takes it."""
    rows = [c for c in rows if c["name"] not in exclude]
    own = [c for c in rows if roles.get(c["name"]) == stage]
    if own and S.ready(own):
        return own
    return [c for c in rows if not roles.get(c["name"])]


def possible(roles, stage, names):
    """The names that could ever take this stage (full or not): its own accounts, or else the general ones."""
    own = [n for n in names if roles.get(n) == stage]
    return own or [n for n in names if not roles.get(n)]


def chain(stages):
    return " -> ".join(NAME[s] for s in stages)
