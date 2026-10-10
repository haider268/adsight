"""Prompt text. The system prompt (role + schema card) is the same for every call of every
question, so providers cache it as a shared prefix; everything per question goes in the
user message."""

ROLE = (
    "You are adsight, a marketing analyst. You answer questions about ad performance by "
    "writing DuckDB SQL over the dataset described below, and you reply with ONE JSON object "
    "matching the requested schema."
)

PLAN_MAX_TOKENS = 2000
ANSWER_MAX_TOKENS = 2000
STEP_MAX_TOKENS = 2000

ANSWER_FIELDS = (
    "Answer fields: kind is answer, clarify or cannot_answer; summary is one or two plain "
    "sentences; values lists every number asked for, each with a label (plain numbers: no "
    "currency signs, ratios as fractions); entities lists ids or names, best first; "
    'direction is up, down or flat for a trend question, else "none"; assumptions lists '
    "any assumption you made (empty if none)."
)

B0_PLAN = (
    "Question: {question}\n\n"
    "Plan your answer. If one SQL query can answer it, return kind query with that single "
    "SELECT in sql. If it is ambiguous, return kind clarify with your clarifying question in "
    "reason. If the data cannot answer it, return kind cannot_answer with the reason. Leave "
    'unused fields as "".'
)

B0_ANSWER = (
    "Question: {question}\n\n"
    "SQL that was run:\n{sql}\n\n"
    "{result}\n\n"
    "Write the final answer from this result only. " + ANSWER_FIELDS
)

B1_STEP = (
    "Question: {question}\n\n"
    "Work step by step. Each turn, either run one SELECT (action run_sql, the query in sql, "
    "and final as an empty answer: kind answer, empty summary and lists, direction "
    '"none") or give the final answer (action final, sql ""). ' + ANSWER_FIELDS + "\n\n"
    "Previous steps:\n{scratchpad}"
)

B1_LAST_STEP = "\n\nYou must give your final answer now."
NO_STEPS = "(none yet)"


def system_prompt(card: str) -> str:
    return ROLE + "\n\n" + card
