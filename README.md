# AutoApply for macOS

AutoApply is a local-first job-search workspace for macOS. It organizes remote job sources, imports structured job reports from Gmail, discovers opportunities with a local Ollama model, and assists with applications through a dedicated Google Chrome profile.

The FastAPI backend, SQLite database, browser automation, and AI model run locally. Website credentials are stored in macOS Keychain; the database contains only opaque Keychain references.

> [!WARNING]
> Browser-assisted applications can submit real forms. Review your candidate profile and test the workflow carefully before using live vacancies. AutoApply leaves CAPTCHA, MFA, missing uploads, and unknown required answers for human review.

## Features

- Manage job-search sites and their login methods.
- Store credentials in macOS Keychain.
- Import vacancies from Gmail messages with the subject `AutoApply Report`.
- Track pending, running, and completed applications in SQLite.
- Discover jobs from `BUSCAR_VAGAS.md` with LlamaIndex and Ollama.
- Generate a ZIP containing a resume, cover letter, and job information for each vacancy.
- Analyze a signed-in LinkedIn profile with the local model.
- Record manual application sessions for future site-specific learning.
- Monitor CPU, memory, disk, Apple GPU identification, and Ollama status.

## Requirements

- macOS Sonoma 14 or newer
- Apple Silicon or an Intel Mac (Ollama uses CPU-only inference on Intel Macs)
- Python 3.11 or newer; Python 3.12 is recommended
- Google Chrome
- [Ollama for macOS](https://docs.ollama.com/macos)
- [Homebrew](https://brew.sh/) and Redis
- Enough free disk space for the selected Ollama model

## Installation

Install Python and Redis with Homebrew:

```bash
brew install python@3.12 redis
```

Install Ollama from its official macOS download, open it once, and allow it to add the `ollama` command to your `PATH` when prompted.

Clone this repository:

```bash
git clone https://github.com/pedrocampos0/autoapply-mac.git
cd autoapply-mac
```

Create the virtual environment and install Python dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create your private local configuration:

```bash
cp .env.example .env
cp data/candidate.example.json data/candidate_profile.json
```

Edit `.env` and set `GMAIL_ACCOUNT`. Replace every example value in `data/candidate_profile.json` with verified candidate information. Both files are ignored by Git.

Download the default local model:

```bash
ollama pull llama2:7b-chat-q2_K
```

You can use another installed model by changing `OLLAMA_MODEL` in `.env`.

## Run with the macOS launcher

Make the launcher executable once:

```bash
chmod +x launcher/start-autoapply.command
```

Start AutoApply from Terminal:

```bash
./launcher/start-autoapply.command
```

You can also double-click `launcher/start-autoapply.command` in Finder after it has executable permission.

The launcher:

1. Loads `.env`.
2. Reuses Ollama or Redis if it finds them on their local ports; otherwise it starts them.
3. Starts FastAPI on `127.0.0.1:8000`.
4. Opens a dedicated Chrome profile with local remote debugging on port `9222`.
5. Stops only the processes it started when the dedicated Chrome process closes.

Logs are written to `logs/backend.log`, `logs/ollama.log`, and `logs/redis.log`.

## Run manually

Use separate Terminal tabs for each process.

Start Ollama and Redis:

```bash
ollama serve
redis-server --bind 127.0.0.1 --port 6379
```

Start Chrome with an isolated user-data directory. Current Chrome releases require a non-default profile for remote debugging:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/AutoApply/ChromeProfile" \
  --new-window http://127.0.0.1:8000/
```

Sign in to Gmail, LinkedIn, and job sites only inside this dedicated profile.

From the repository root, start the API:

```bash
source .venv/bin/activate
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Health information is available at <http://127.0.0.1:8000/api/status>.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `GMAIL_ACCOUNT` | For Gmail imports | None | Account expected in the dedicated Chrome profile. |
| `OLLAMA_MODEL` | No | `llama2:7b-chat-q2_K` | Local model used by chat and automation. |
| `OLLAMA_URL` | No | `http://127.0.0.1:11434` | Ollama API base URL. |
| `OLLAMA_NUM_GPU` | No | `24` | Preferred number of model layers offloaded to the GPU. |
| `LINKEDIN_PROFILE_URL` | No | None | Exact LinkedIn profile URL if automatic navigation cannot find it. |
| `AUTOAPPLY_PROFILE_FILE` | No | `data/candidate_profile.json` | Alternate private candidate-profile file. |
| `AUTOAPPLY_CHROME_PROFILE` | Launcher only | `~/Library/Application Support/AutoApply/ChromeProfile` | Dedicated Chrome data directory. |
| `AUTOAPPLY_CHROME_EXECUTABLE` | Launcher only | Standard `/Applications` path | Alternate Google Chrome executable. |

## Project structure

```text
backend/                      FastAPI API and automation services
frontend/                     Static dashboard
launcher/start-autoapply.command
                              macOS service launcher
data/candidate.example.json   Safe candidate-profile template
BUSCAR_VAGAS.md               Job discovery rules and filters
requirements.txt              Python dependencies
```

Runtime state is stored under `data/`, `logs/`, and `tmp/`. The real candidate profile, `.env`, SQLite files, recordings, browser data, and logs are excluded from version control.

## Privacy and security

- The API, Ollama, Redis, and Chrome debugging endpoints use loopback addresses.
- Credentials are stored in macOS Keychain through Python `keyring`.
- Candidate identity data remains in a local ignored JSON file.
- AI prompts are sent to the configured Ollama server, not to OpenAI.
- Chrome automation uses a dedicated profile instead of your normal browsing profile.
- Logs may contain prompts, model responses, job details, and error traces; treat them as sensitive.
- macOS may ask whether Python can access an `AutoApply` Keychain item. Approve access for the Python executable inside this project's virtual environment.

## Validation

Check Python and JavaScript syntax:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q backend
node --check frontend/assets/app-v2.js
zsh -n launcher/start-autoapply.command
```

## Troubleshooting

- **`python3.12` is not found:** run `brew --prefix python@3.12` and ensure its `bin` directory is on `PATH`, or use a supported `python3` installation.
- **Keychain access fails:** open Keychain Access, confirm the login keychain is unlocked, and retry from the same virtual environment.
- **Candidate profile is missing:** copy `data/candidate.example.json` to `data/candidate_profile.json` and fill it in.
- **Gmail account is rejected:** ensure `GMAIL_ACCOUNT` matches the account signed in to the dedicated Chrome profile.
- **Chrome automation is offline:** close the dedicated Chrome instance, then restart it through the launcher.
- **Ollama is unavailable:** open the Ollama app or run `ollama serve`, and verify that your configured model is installed.
- **Redis is unavailable:** run `brew install redis`; the launcher will start a project-local Redis process.
- **Port 8000 is busy:** stop the existing process before starting the launcher.

## Platform notes

The macOS dashboard reports the Apple GPU name when available. macOS does not expose a stable per-process GPU utilization metric through the APIs used here, so GPU percentage and dedicated VRAM are not reported for Apple Silicon. Memory metrics include the system's unified memory through `psutil`.
