# Outreach

Drains the lead queue the Lead Finder fills: assesses each prospect's site,
enriches it, drafts a specific first email, sends it through Brevo, and follows
up once. Runs on Modal on weekday mornings.

It is a **pure consumer of `core.leads`** — it discovers nothing and writes no
lead of its own. One writer per table is what makes "no duplicates, nothing
missed" a property of the schema rather than a promise in a document.

Every draft cites a **real** WizCodes project with a real URL, pulled from the
site repo, never from the model's memory. A draft that cannot ground itself is
not sent.

---

## Setup

```powershell
python -m venv .venv                                   # Python 3.11
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\wizcore
copy .env.example .env                                 # then fill it in
```

Never edit `.env` by hand in PowerShell — it mangles the encoding. Use:

```powershell
python tools/set_env.py KEY=VALUE
```

## Check it works

```powershell
python tools/preflight.py --live       # ends with: outreach_agent: ready.
python main.py --assess https://example.com   # one site, printed, no DB writes
python main.py --health                # sending health: bounces, complaints, caps
```

## Run

```powershell
python main.py --dry-run               # full run, drafts everything, sends nothing
python main.py --followups --dry-run   # the touch-2 sweep only
python main.py                         # honours DRY_RUN from .env
```

`--dry-run` still calls PageSpeed and the LLM — it is a real rehearsal with the
send suppressed, which is the only kind worth trusting.

## Deploy

```powershell
.\deploy.ps1              # preflight -> migrations -> secret -> deploy
.\deploy.ps1 -DryRun      # print the plan, touch nothing
```

Two schedules, deliberately separate: **07:00 Mon–Fri** first touch, **09:00
Mon–Fri** follow-ups. A follow-up must never ride the same run as the first
touch that would have created it.

## Before the first real send

1. Confirm DKIM is green in Brevo and send one seed email to yourself — read
   the `Authentication-Results` header rather than trusting the dashboard.
2. `OUTREACH_FROM_EMAIL` must be a **verified Brevo sender**. The authenticated
   domain is `wizcodes.site`; `mail.wizcodes.site` is a bounce handler and its
   address will be rejected on every send.
3. Start with `DAILY_SEND_CAP` low. Reputation is earned slowly and lost once.

## Replies

Brevo reports opens, clicks, bounces and complaints — **not replies**, which
land in the real inbox. When someone answers, tell the agent so it stops
chasing them:

```
/replied <lead_id>     stop the sequence, mark as engaged
/done <lead_id>        close it out
/stop <lead_id>        suppress permanently
```

## Configuration that matters

| Variable | Why |
|---|---|
| `DRY_RUN` | The kill switch. Drafts and assesses, sends nothing. |
| `OUTREACH_FROM_EMAIL` | Must be verified in Brevo or every send fails. |
| `MAX_EMAILS_PER_DAY` | The single most important number here. Start low. |
| `MAX_EMAILS_PER_RUN` | Per-run cap, under the daily one. |
| `MIN_WEAKNESS_SCORE` | Floor for entering the sequence — a site with nothing wrong gets no email. |
| `FOLLOWUP_AFTER_DAYS` | Gap before touch 2. `MAX_SEQUENCE_STEPS=2` — there is no touch 3 by design. |
| `MAX_BOUNCE_RATE` / `MAX_COMPLAINT_RATE` | Sending halts itself above these. |
| `EMAIL_PROVIDER` | `brevo`. |

## Layout

```
assess/      psi · fingerprint · score · vision (disabled: the proxy strips images)
enrich/      contact discovery + company context
compliance/  suppression · unsubscribe · rate limits
prompts/     draft + follow-up, with the grounding contract
channels/    brevo send · brevo events · telegram commands · manual hand-off
graph/       LangGraph state, nodes, build, followup sweep
```

See `EMAIL_PLAYBOOK.md` for the voice rules and what makes a draft sendable.
