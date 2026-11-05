# Market Stretch Terminal

End-of-day overbought / oversold monitor for the Nasdaq-100 (QQQM), S&P 500 (SPY) and Dow (DIA).
No API key needed. Not live: a GitHub Action rebuilds the data after each US close.

## Folder layout (what the repo should look like)

```
market/                         <- your repo (name it whatever you like)
├── index.html                  <- the whole app
├── README.md
├── data/
│   └── market.json             <- created by the Action; you do NOT add this
├── scripts/
│   ├── update_data.py
│   └── requirements.txt
└── .github/                    <- note the "h": this is NOT the .git folder
    └── workflows/
        └── update-data.yml
```

`.git` is the hidden folder git creates for you automatically when you run `git init`/`git clone`. You never create it.
`.github` is a normal folder you add; GitHub looks inside it for workflows.

## Setup (command line, same as your other repos)

```bash
unzip market-stretch-terminal.zip -d market && cd market
git init -b main
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<you>/market.git   # create the empty repo on github.com first
git push -u origin main
```

Then on github.com:
1. Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)`.
2. Actions tab → "Update market data" → **Run workflow**. First run takes a few minutes; it commits `data/market.json`.
3. If the run fails at the commit step: Settings → Actions → General → Workflow permissions → **Read and write permissions**.
4. Open `https://<you>.github.io/market/`.

Web-upload alternative: on github.com choose Add file → Upload files and drag in `index.html`, `README.md`, `scripts/`.
For the hidden `.github` folder, use Add file → Create new file, type `.github/workflows/update-data.yml` as the name, and paste the file contents.

## Optional
- Repo secret `FMP_API_KEY`: only used as a last-resort backup source for the three index ETFs if Yahoo and Stooq both fail.
- A free FMP key can also be pasted into the app's Menu to fetch price history in manual mode (browsers may block it; see the note there).
- Base cases: the thresholds live in `PRESETS` near the top of the script in `index.html`. The "what followed past signals" table uses `PRESET_RULES` in `scripts/update_data.py`; keep the two in step.

## If something looks wrong
- Header says data is N days old: the Action failed or Yahoo blocked it that day. Check the Actions log and re-run.
- Site not updating after a successful run: confirm Pages is deploying from the branch the Action commits to.
- `yfinance` and Wikipedia are unofficial sources and can change without notice. If Wikipedia's tables change, the script reuses the previous constituent list.
- Nasdaq-100 top-10 weight is approximate (market-cap share). S&P 500 weights are not computed.
- Without `data/market.json` the page falls back to manual entry (Update data menu).
