# Parley

AI procurement negotiation desk. A game-theory engine ([NegMAS](https://github.com/yasserfarouk/negmas))
decides the action and price each round; an LLM (Sarvam or DeepSeek) only words the reply.
Guardrails discard any wording that leaks your target/ceiling or changes the price.

Features: deal brief, supplier link room, live buyer desk (whisper / take over / extend / conclude),
NegMAS forecast with strategy chart, PDF report, backup/restore of your deal list.

## Run locally
```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Configuration (env vars)
| Var | Purpose |
|---|---|
| `SARVAM_API_KEY` / `DEEPSEEK_API_KEY` | LLM wording. With neither, a rules engine is used. |
| `AI_PROVIDER` | `sarvam` or `deepseek` (optional) |
| `SARVAM_MODEL` / `DEEPSEEK_MODEL` | model overrides |
| `DATA_DIR` | SQLite folder (default `data`). Render's free tier disk is ephemeral. |

## Deploy on Render
Blueprint: `render.yaml`. Add the API key in the service's Environment tab.
