# Deploying Miftah Alkhazina (free)

Three free services:

- **GitHub:** stores the code.
- **Supabase:** the database.
- **Render:** runs the site and gives it a URL.

> ⚠️ For demos and testing only. Supabase and Render servers are outside Saudi Arabia, so do not enter real customer data before reviewing data-protection requirements.

## 1. GitHub: push the code

1. Sign up at https://github.com (free).
2. From the **+** button choose **New repository**.
   - Name: `miftah-alkhazina`
   - Choose **Private**.
   - Do not add a README or .gitignore.
3. Copy the repository URL, e.g. `https://github.com/USERNAME/miftah-alkhazina.git`, and push the code to it.

## 2. Supabase: the database

1. Sign up at https://supabase.com (free).
2. Click **New project**:
   - Name: `miftah-alkhazina`
   - **Database password:** click Generate, then copy and keep it.
   - **Region:** choose **Central EU (Frankfurt)**.
3. When the project is ready (about two minutes), click **Connect** at the top.
4. Choose **Session pooler** (important: not Direct connection, which is IPv6-only and does not work from Render) and copy the URL. It looks like:
   `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres`
5. Replace `[YOUR-PASSWORD]` with the database password from step 2.

> 🔒 This URL contains the database password. **Never paste it into a chat.** Paste it into Render yourself in the next step.

## 3. Render: run the site

1. Sign up at https://render.com with the same GitHub account.
2. From **New +** choose **Blueprint**, then the `miftah-alkhazina` repository. Render reads `render.yaml` automatically.
3. It asks for four values:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Supabase URL from step 2 (with the password) |
   | `ADMIN_USERNAME` | the admin login name, e.g. `admin` |
   | `ADMIN_PASSWORD` | a temporary admin password (8+ characters); it must be changed on first login |
   | `DEMO_PASSWORD` | password for the demo accounts (`demo-acc` accountant and `demo-rev2` reviewer) |

4. Click **Apply**. The first deploy takes 3 to 5 minutes.
5. The URL appears at the top, e.g. `https://miftah-alkhazina.onrender.com`.

## 4. Sharing with stakeholders

- **Link:** the Render URL.
- **Quick demo:** sign in as `demo-acc` (accountant) with `DEMO_PASSWORD`. There is a demo company with an approved policy and transactions.
- **To try approvals:** sign in as `demo-rev2` (reviewer).
- **A personal account:** sign in as the admin, open **Users**, and add an accountant.

## 5. Keep the site awake

Render's free plan **sleeps after 15 idle minutes**, and the first visit after that takes about a minute. Three things keep it awake:

- **The site pings itself** every 10 minutes while running (`app/main.py`, using `RENDER_EXTERNAL_URL`, which Render sets automatically).
- **A GitHub Actions workflow** (`.github/workflows/keepalive.yml`) visits `/keepalive` every 5 minutes as a backup. GitHub may delay scheduled runs, and disables them after 60 days without repository activity.
- Optionally, **UptimeRobot** (https://uptimerobot.com, free): add an HTTP(s) monitor for `https://miftah-alkhazina.onrender.com/keepalive` every 5 minutes.

`/keepalive` also runs a trivial database query, which keeps the free Supabase project from pausing after a week, and runs any payouts that are due.

Render's free hours (750 per month) cover one service running all month. The paid Starter plan never sleeps.

## 6. Confirmation and password-reset email (Brevo, free)

Self-signup sends a confirmation link by email. Without this step, signups are verified automatically and links are printed to the Render logs instead.

Note: Render's free plan blocks SMTP ports, so email goes through Brevo's HTTPS API.

1. Sign up at https://www.brevo.com (free plan: 300 emails a day).
2. **Verify the sender:** **Senders, Domains & Dedicated IPs** → **Senders** → **Add a sender**, and confirm the link they email you.
3. **API key:** **SMTP & API** → **API Keys** → **Generate a new API key**. Copy it (it is shown once).
4. In Render → **Environment** add:
   - `BREVO_API_KEY` = the key
   - `MAIL_FROM` = the address verified in step 2
5. **Save Changes**.

> 🔒 The API key is secret. Never share it or paste it into a chat; only into Render.

## Notes

- If Supabase pauses anyway, restore it from the Supabase dashboard with **Restore**.
- Every push to GitHub is deployed by Render automatically.
- Don't want the demo company? Set `DEMO_SEED` to `0` in the Render settings.
