# SalesQuest ERP

Cloud app: https://popeyaiartificia-png.github.io/sales-erp/

The frontend is published from `main` through GitHub Pages. Supabase project
`sbxtmkhbtbkajvqfbbnf` hosts PostgreSQL, account authentication, the API, and the
scheduled messaging worker. The browser contains only the publishable API key.
Private ERP tables and privileged functions are restricted to the server role;
the API checks both the signed-in user and `salesquest_members`.

Initial owner login details are saved locally in `salesquest_data/cloud-login.json`.
This file, runtime credentials, databases, and backups are excluded from Git.
Public signups are disabled. Add authorized accounts to `salesquest_members`
through the Supabase dashboard before granting them ERP access.

## Local app

Double-click `Start SalesQuest.cmd`, or run `python server.py --open`.
The local app continues to use SQLite in `salesquest_data/`. Cloud data and local
SQLite data are separate; use the cloud app for shared work across devices.
Git synchronization updates source code, not either database.

## Deploy changes

Run these commands from this project folder after signing into Supabase:

```powershell
npx.cmd --yes supabase@3.2.0 login
npx.cmd --yes supabase@3.2.0 link --project-ref sbxtmkhbtbkajvqfbbnf
npx.cmd --yes supabase@3.2.0 db push --remote --dry-run
npx.cmd --yes supabase@3.2.0 db push --remote --yes
npx.cmd --yes supabase@3.2.0 functions deploy salesquest-api salesquest-worker --use-api --yes
```

Commit and push source changes to `main` to update GitHub Pages. Future source
syncs are manual, as requested: commit local edits, pull with rebase, then push.

The worker uses the server-only `SALESQUEST_WORKER_TOKEN` secret. Its matching
value is encrypted in Supabase Vault for the `salesquest-worker` Cron job, which
runs once per minute. `SALESQUEST_ALLOWED_ORIGINS` restricts the API's browser
origins; `SALESQUEST_APP_URL` supplies the link in reminder emails. Their local
deployment copy is `salesquest_data/cloud.env`, which is excluded from Git.

Sending windows, daily caps, opt-outs, and reminder dates use `Asia/Kolkata`.
Interrupted deliveries are marked for review rather than retried automatically.
Configure WhatsApp and SMTP credentials in the cloud app before sending.
Cloud SMTP accepts ports 465, 2525, or 2587; use 465 for Gmail.

## Import and backups

`python scripts/import_supabase.py` previews the local SQLite state import.
To apply it, set `SUPABASE_SECRET_KEY` in the current process and add `--apply`.
The tool targets this project only, backs up SQLite, excludes credentials and
message queues, refuses to overwrite different cloud data, and verifies every
imported field. Repeating an identical import is safe.

The current Supabase Free plan does not include managed daily backups. Export
ERP backups regularly from Settings. Original VCCI profile files and scanned ads
were absent from the supplied folder; existing imported VCCI leads are retained.

## Verification

```powershell
python -m unittest discover -s tests -v
node --test tests/cloud_domain.test.mjs
npx.cmd --yes deno check supabase/functions/salesquest-api/index.ts supabase/functions/salesquest-worker/index.ts
```

PostgreSQL integration tests create and stop a disposable localhost instance.
Set `POSTGRES_BIN` if PostgreSQL is installed outside its standard Windows path.
