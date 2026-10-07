# BubbleSheetTools

Create bubble sheets for multiple choice (or partly multiple choice) exams, grade the
scanned sheets, and export the results as CSV files and PNG charts.

- **Create sheets** (no account): enter a class name, exam name, number of questions
  (up to 100), answer choices per question (2–10) and optional numbered written-response
  boxes of any height. Paste student names or upload a CSV. You get one PDF: page 1 is
  the **answer key sheet**, then one personal sheet per student with a QR code.
- **Grade exams** (teacher account): drag and drop the filled-in key sheet (or a CSV key),
  then student scans (PDF, JPG, PNG or HEIC), in one batch or several. Every page is
  identified by its QR code, so one PDF holding both the key sheets and the students'
  sheets loads the key and grades the students in a single upload. Bubbles the
  scanner can't read confidently are listed with a picture of the row so you can fix
  them by hand. Enter hand-graded written scores, then download:
  - `results.csv`: one row per student, with every answer
  - `item_analysis.csv`: % correct, # missed and choice counts per question
  - `score_distribution.png`, `most_missed.png`, `choice_distribution.png`
- **Paste or upload your answer key** when creating sheets (optional): one answer per line
  (`B`, `AC`, `FR` for free response), numbered lines, a CSV, or a markdown table. The key
  sheets come pre-filled, and the form's question count, open questions and choices are set
  from the key. When grading, upload the downloaded PDF itself as the key; only its key
  sheets are read, so nothing has to be printed or scanned.
- **Exam versions** (up to 4): with 2 or more, every sheet gets a "Version" bubble row
  and the PDF starts with one answer key sheet per version. Each student is graded against
  the key for the version they bubbled; a blank or double-marked version is flagged for
  the teacher to pick. Keys can be scanned key sheets (one PDF can hold all of them), a
  CSV per version, or one CSV with `Question, Version 1, Version 2, ...` columns. Question
  analysis and the per-question charts are produced **per version**, so scrambled
  questions or choices don't matter.
- **Open questions** (e.g. `5, 12-14`): graphing or free-response problems inside the
  numbering. Their rows print an "open response" box where the bubbles would be, so
  nothing can be bubbled there. The scanner skips them; the teacher types a score for each
  in the results table (or in `results.csv` and re-uploads it). Open scores count toward
  the total but not the multiple choice score, the question analysis or the charts. In a
  CSV key, write `open` (or `-`) for these questions.
- **Continue from a results CSV** (teacher account): re-upload a `results.csv` from
  earlier to regenerate the analysis (lost downloads), grade late students' sheets from the
  same print batch, or combine several periods that used the same answer key. A student
  who appears again replaces their earlier row, with a note.

## Privacy (COPPA)

- **Student information is never stored.** Names are used only to print the sheets.
  During grading, names (read from QR codes), answers and scan snippets are kept **in
  server memory only**. They are deleted when the teacher clears the session, signs out,
  or is inactive for `SESSION_TTL_MINUTES` (default 120), and whenever the server
  restarts. Uploaded files are never written to disk.
- Teacher accounts are handled by Supabase and store only first name, last name, email
  and a confirmation that the teacher is 18 or older.
- Supabase keys stay on the server. The browser never receives them, and sign-in tokens
  are kept in HttpOnly cookies.

## How it works

Each sheet has four black corner squares and a QR code. The QR code holds the exam
details, the student's name and roster number, and the page number, so any page can be
read on its own. The scanner:

1. finds the corner squares and straightens the page (it handles tilted phone photos,
   upside-down or sideways pages, and uneven lighting);
2. reads the QR code;
3. measures how dark each bubble is compared with that page's empty bubbles and its
   typical pencil mark.

A question is flagged for review when a bubble is faint or partly erased, when more
than one bubble is filled on a single-answer question, or when it is blank.

Questions can have several correct answers: fill in all of them on the key sheet, or
write `2,AC` in a CSV key. On the results screen, choose whether students must mark
**all** of the correct answers, or whether **any** correct answer counts (as long as no
wrong answer is marked).

## Project layout

```text
app/
  main.py             FastAPI routes
  config.py           settings from environment variables
  auth.py             Supabase sign-up / sign-in (server side only)
  sessions.py         in-memory grading sessions with expiry
  roster.py           parse pasted names / CSV rosters
  answer_key.py       parse CSV answer keys
  results_import.py   read a results.csv back in (restore / combine / late scans)
  sheet_layout.py     one shared definition of sheet geometry and QR contents
  sheet_generator.py  build the PDF (ReportLab + qrcode)
  scanner/
    loader.py         PDF / image / HEIC -> page images
    align.py          find corner squares, straighten page, orientation
    qr.py             QR decoding
    bubbles.py        bubble darkness -> marked / unclear / empty
  grading.py          compare sheets to the key, flags, scoring
  export.py           CSV files and PNG charts (matplotlib)
static/               index.html, app.css, app.js (plain HTML/CSS/JS)
tests/                unit tests + end-to-end tests with simulated scans
```

## Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env            # then edit .env
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. To try grading without Supabase, set `AUTH_DEV_BYPASS=1` in
`.env`. This signs every visitor in as a test teacher, so **use it only on your own
computer**. The app refuses to start with it when `ENVIRONMENT=production`.

Run the tests with `python -m pytest`.

> On district-managed Windows laptops, Application Control may block the HEIC library.
> The app still runs, and HEIC uploads show a message asking for JPG instead. HEIC works
> normally on the Linux server.

## Supabase setup

1. Create a project at <https://supabase.com>.
2. **Authentication → Sign In / Providers → Email**: enable email sign-up and keep
   **Confirm email** turned on.
3. **Authentication → URL Configuration**: set **Site URL** to your site, e.g.
   `https://bubblesheets.example.com/`, and add it under **Redirect URLs** (add
   `http://127.0.0.1:8000/` too for local testing).
4. **Project Settings → API Keys**: copy the project URL and the **publishable** key
   (older projects call it the **anon** key) into `.env` as `SUPABASE_URL` and
   `SUPABASE_ANON_KEY`. **Do not use the secret / service_role key**; this app doesn't
   need it.

The teacher's first and last name and the 18+ confirmation are saved as the user's
metadata in Supabase Auth. No database tables are needed.

## Deploy on a DigitalOcean droplet

1. Create an Ubuntu droplet (1 GB RAM is enough to start; 2 GB is more comfortable for
   large batches) and install Docker: <https://docs.docker.com/engine/install/ubuntu/>
2. Point your domain's DNS **A record** at the droplet's IP address.
3. On the droplet:

   ```bash
   git clone https://github.com/smithbwlhs/BubbleSheetTools.git
   cd BubbleSheetTools
   cp .env.example .env && nano .env      # set SUPABASE_*, ENVIRONMENT=production
   nano Caddyfile                         # replace bubblesheets.example.com with your domain
   docker compose up -d --build
   ```

   Caddy obtains an HTTPS certificate automatically.
4. Update: `git pull && docker compose up -d --build`.

The app must run as **one process** (the Dockerfile sets `--workers 1`), because grading
sessions live in memory.

## Printing and scanning tips

- Print at **100% / actual size** (not "fit to page") on US Letter paper.
- Students should use a dark pencil (No. 2) and fill bubbles completely.
- A copier's scan-to-PDF at 200–300 dpi works best. Phone photos work too: keep the
  whole page and all four corner squares in the frame, and avoid strong shadows.

## License

GPL-3.0. PyMuPDF (used to read PDFs) is AGPL-licensed. Running this as a public web
service is fine because the source code is published here.
