# Trello → GitHub Migrator

A Streamlit web application that migrates Trello boards to GitHub Issues and Projects. Built as a portfolio project demonstrating REST/GraphQL API integration, state management, and clean UI design.

## Features

- **Two Input Modes**:
  - **API Mode**: Connect directly to your Trello account and browse all your boards
  - **JSON Upload Mode**: Upload a Trello board export file (no API credentials needed)
- **Board Preview**: View all lists and cards before migration with an expandable interface
- **Status Mapping**: Map Trello lists to GitHub Project status fields with a visual interface
- **Smart Migration**:
  - Creates GitHub repository automatically (if it doesn't exist)
  - Converts Trello cards → GitHub Issues
  - Adds issues to GitHub Projects v2
  - Sets Project status based on Trello list (via GraphQL API)
  - Preserves and creates labels with matching colors
  - Links back to original Trello cards
- **Real-time Progress**: Watch the migration happen with a live progress bar
- **Error Handling**: Comprehensive error reporting with real-time display

## Tech Stack

- **Python 3.9+**
- **Streamlit** - Web UI framework
- **Requests** - HTTP client for API calls
- **GitHub REST API** - Repository, issues, and labels
- **GitHub GraphQL API** - Projects v2 integration
- Direct API integration (no external migration libraries)

## Getting Started

### Prerequisites

- Python 3.9 or higher
- A Trello account with at least one board
- A GitHub account with a Project (Projects v2)

### Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/JuanitaOssa/trello-to-github.git
   cd trello-to-github
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Run the application**

   ```bash
   streamlit run app.py
   ```

   The app will open in your browser at `http://localhost:8501`

## Getting Your Trello Data

You have two options for importing your Trello board:

### Option 1: JSON Export (Recommended for one-time migrations)

No API credentials needed! Simply export your board as JSON:

1. Open your Trello board in a web browser
2. Click **Show Menu** (top right corner)
3. Click **More** to expand additional options
4. Click **Print and Export**
5. Click **Export as JSON**
6. Save the downloaded `.json` file
7. Upload it in the app using the file uploader

### Option 2: Trello API (For multiple boards or automation)

1. Go to the [Trello Power-Ups Admin Portal](https://trello.com/power-ups/admin)
2. Click **"New"** to create a new Power-Up (or select an existing one)
3. Fill in the required fields:
   - **Name**: "GitHub Migrator" (or any name you prefer)
   - **Workspace**: Select your workspace
4. After creation, you'll see your **API Key** on the Power-Up page
5. Click the **"Generate a Token"** link next to the API key
6. Click **"Allow"** to authorize the app
7. Copy the **Token** that appears on the next page

> **Note**: Keep both the API Key and Token secure. They provide full access to your Trello account.

## GitHub Setup

### Personal Access Token

1. Go to [GitHub Settings → Developer Settings → Personal Access Tokens](https://github.com/settings/tokens)
2. Click **"Generate new token"** → **"Generate new token (classic)"**
3. Configure the token:
   - **Note**: "Trello Migration" (or any descriptive name)
   - **Expiration**: Choose based on your needs
   - **Scopes**: Select these permissions:
     - `repo` - Full control of private repositories
     - `read:project` - Read access to projects
     - `project` - Full control of projects
4. Click **"Generate token"**
5. **Copy the token immediately** - you won't be able to see it again!

Or use this direct link: [Generate token with required scopes](https://github.com/settings/tokens/new?scopes=repo,read:project,project)

### GitHub Project Setup

1. Create a GitHub Project (Projects v2) in your account or organization
2. Add a **Status** field to the project (usually exists by default)
3. Configure status options to match your Trello lists (e.g., "To Do", "In Progress", "Done")
4. Note the **Project Number** from the URL (e.g., `18` from `github.com/users/you/projects/18`)

## How It Works

### Migration Flow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Trello Board   │────▶│   This App      │────▶│  GitHub Repo    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                       │                       │
        ▼                       ▼                       ▼
   ┌─────────┐            ┌───────────┐           ┌───────────┐
   │  Lists  │───────────▶│   Map     │──────────▶│  Status   │
   └─────────┘            └───────────┘           └───────────┘
        │                       │                       │
        ▼                       ▼                       ▼
   ┌─────────┐            ┌───────────┐           ┌───────────┐
   │  Cards  │───────────▶│ Transform │──────────▶│  Issues   │
   └─────────┘            └───────────┘           └───────────┘
        │                       │                       │
        ▼                       ▼                       ▼
   ┌─────────┐            ┌───────────┐           ┌───────────┐
   │ Labels  │───────────▶│   Match   │──────────▶│  Labels   │
   └─────────┘            └───────────┘           └───────────┘
```

### What Gets Migrated

| Trello | → | GitHub |
|--------|---|--------|
| Board | → | Repository |
| List | → | Project Status |
| Card | → | Issue (added to Project) |
| Card Title | → | Issue Title |
| Card Description | → | Issue Body |
| Card Labels | → | Issue Labels |
| Card URL | → | Reference in Issue Body |

### Color Mapping

Trello label colors are automatically mapped to GitHub label colors:

| Trello | GitHub |
|--------|--------|
| Green | `#0e8a16` |
| Yellow | `#fbca04` |
| Orange | `#d93f0b` |
| Red | `#b60205` |
| Purple | `#5319e7` |
| Blue | `#0052cc` |
| Sky | `#1d76db` |
| Lime | `#84b817` |
| Pink | `#e99695` |
| Black | `#333333` |

## Usage

### Step 1: Load Trello Data

**Using JSON Upload Mode:**
1. Select "Upload Board JSON" in the sidebar
2. Export your board from Trello as JSON
3. Upload the `.json` file

**Using API Mode:**
1. Select "Connect via Trello API" in the sidebar
2. Enter your Trello API key and token
3. Click "Connect to Trello"
4. Select a board and click "Load Board Data"

### Step 2: Configure GitHub

1. Enter your GitHub Personal Access Token
2. Select account type (Personal or Organization)
3. Enter your username/org name
4. Enter the target repository name
5. Enter the Project Number

### Step 3: Map Status Fields

1. The app fetches your GitHub Project's Status field options
2. For each Trello list, select the corresponding GitHub Status
3. Lists are auto-matched by name when possible
4. Click "Confirm Mapping" to proceed

### Step 4: Migrate

1. Review the migration plan
2. Click "Start Migration"
3. Watch real-time progress as issues are created and added to the project
4. View the results summary with links to your new repo

## Screenshots

<!-- Add your screenshots here -->

### Credential Input
![Credential Input](screenshots/credentials.png)

### Board Preview
![Board Preview](screenshots/preview.png)

### Status Mapping
![Status Mapping](screenshots/mapping.png)

### Migration Progress
![Migration Progress](screenshots/progress.png)

### Results Summary
![Results Summary](screenshots/results.png)

## Environment Variables (Optional)

You can pre-fill credentials using environment variables. Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
```

Then edit `.env` with your credentials. The app will still allow manual input for any missing values.

## Limitations

- **Rate Limits**: GitHub has API rate limits. Large boards may require multiple runs.
- **Attachments**: Trello card attachments are not migrated (linked in issue body as text only)
- **Comments**: Trello card comments are not migrated
- **Checklists**: Trello checklists are not migrated (consider using GitHub task lists in the future)
- **Due Dates**: Trello due dates are not mapped
- **Project Required**: You must have an existing GitHub Project with a Status field

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is open source and available under the [MIT License](LICENSE).

## Acknowledgments

- Built with [Streamlit](https://streamlit.io/)
- Uses the [Trello REST API](https://developer.atlassian.com/cloud/trello/rest/)
- Uses the [GitHub REST API](https://docs.github.com/en/rest)
- Uses the [GitHub GraphQL API](https://docs.github.com/en/graphql) for Projects v2
