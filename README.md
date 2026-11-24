# MotoGP Results Agent

A Python portfolio project in development. Give the agent a MotoGP classification link and it can collect the available PDF reports for that session or race weekend. You can also request a full season. The Google ADK agent uses a local Gemma model through Ollama by default and can also use the Google Gemini API. The same downloader runs directly from the command line.

## Try one session

From the project directory, create a virtual environment and install the dependencies. These commands use Windows PowerShell and Python 3.13.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

To download reports for the first free practice session of the Japanese Grand Prix, run the CLI. This workflow only needs Python and an internet connection.

```powershell
$url = "https://www.motogp.com/en/gp-results/2026/jpn/motogp/fp1/classification"
python motogp_pdf_tools.py --session $url
```

Look in `~/MotoGP_Results/2026/Jpn/fp1/` for the PDFs, where `~` is your home directory. The command prints counts for downloaded, existing and failed files, plus the path to a detailed JSON report. Running it again skips files already present.

To collect the other sessions from the same weekend, use `python motogp_pdf_tools.py --weekend $url`. To collect every race weekend in a season, use `python motogp_pdf_tools.py --season 2026`. Both depend on report availability in the undocumented MotoGP results API.

## Run the conversational agent

### Local Gemma through Ollama (default)

The configuration in `agent.py` uses Gemma 4 E2B, a compact model with an effective size of 2 billion parameters. Install and start Ollama locally, then pull the model with the command below. The agent connects to `http://localhost:11434`. [Gemma 4 E2B on Ollama](https://ollama.com/library/gemma4:e2b)

```powershell
ollama pull gemma4:e2b
```

### Google Gemini API

In `agent.py`, replace the entire `model=LiteLlm(...),` argument inside `root_agent = Agent(...)` with the following line. It selects Gemini Flash through ADK’s built-in Gemini integration. [Gemini model IDs](https://ai.google.dev/gemini-api/docs/models)

```python
model="gemini-flash-latest",
```

Create or edit `.env` next to `agent.py` and enter your Gemini API key in place of the placeholder. The project’s `.gitignore` excludes this file.

```dotenv
GOOGLE_GENAI_USE_ENTERPRISE=false
GOOGLE_API_KEY=your_gemini_api_key_here
```

### Start the agent

With the virtual environment active and the project folder named `MotoGP_Agent`, start the ADK terminal session from its parent directory.

```powershell
cd ..
adk run MotoGP_Agent
```

Example request to the agent:

> Download all available PDF reports for the race weekend containing this session: https://www.motogp.com/en/gp-results/2026/jpn/motogp/fp1/classification

You can also ask the agent to create the session folders for a weekend or season before downloading any reports.

## Next phase

The next phase will extract structured racing data from the downloaded PDFs. The first planned use case is reading a session classification into a table so that results can be compared across race weekends.
