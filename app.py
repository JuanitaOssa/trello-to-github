"""
Trello → GitHub Migrator
A Streamlit app to migrate Trello boards to GitHub Issues and Milestones.
"""

from __future__ import annotations

import streamlit as st
import requests
from typing import Optional, Union
import time
import json
import re
import unicodedata


# =============================================================================
# HELPERS
# =============================================================================

def clean_title(title: str) -> str:
    """
    Clean a title string for use in GitHub API calls.

    - Strips leading/trailing whitespace
    - Removes zero-width and invisible unicode characters
    - Keeps standard emojis intact
    """
    if not title:
        return title

    # Strip whitespace
    title = title.strip()

    # Remove zero-width and invisible characters
    # These include: zero-width space, zero-width non-joiner, zero-width joiner,
    # left-to-right mark, right-to-left mark, and other format characters
    invisible_chars = re.compile(
        r'[\u200b\u200c\u200d\u200e\u200f'  # Zero-width chars and direction marks
        r'\u2060\u2061\u2062\u2063\u2064'    # Word joiner, invisible operators
        r'\ufeff'                            # Byte order mark
        r'\u00ad'                            # Soft hyphen
        r'\u034f'                            # Combining grapheme joiner
        r'\u061c'                            # Arabic letter mark
        r'\u115f\u1160'                      # Hangul fillers
        r'\u17b4\u17b5'                      # Khmer vowel inherent
        r'\u180b-\u180e'                     # Mongolian free variation selectors
        r'\uffa0'                            # Halfwidth Hangul filler
        r'\ufff0-\ufff8'                     # Specials
        r']'
    )
    title = invisible_chars.sub('', title)

    # Normalize unicode to NFC form (composed characters)
    title = unicodedata.normalize('NFC', title)

    # Remove any remaining control characters (but keep emojis)
    # Control chars are in categories Cc and Cf, but we want to keep some Cf for emojis
    cleaned = []
    for char in title:
        category = unicodedata.category(char)
        # Keep everything except control characters (Cc)
        # and format characters (Cf) that aren't emoji-related
        if category != 'Cc' and (category != 'Cf' or ord(char) > 0xFFFF):
            cleaned.append(char)

    return ''.join(cleaned).strip()


# =============================================================================
# TRELLO JSON PARSER
# =============================================================================

def parse_trello_json(json_data: dict) -> dict:
    """
    Parse a Trello board JSON export into the format used by the app.

    Trello exports contain the full board data including lists, cards, and labels.
    """
    board_name = json_data.get("name", "Imported Board")
    board_id = json_data.get("id", "imported")

    # Extract lists (filter out archived lists)
    lists = [
        {"id": lst["id"], "name": lst["name"]}
        for lst in json_data.get("lists", [])
        if not lst.get("closed", False)
    ]

    # Build a map of label IDs to label info
    label_map = {
        label["id"]: {"name": label.get("name", ""), "color": label.get("color", "")}
        for label in json_data.get("labels", [])
    }

    # Extract cards (filter out archived cards)
    cards = []
    for card in json_data.get("cards", []):
        if card.get("closed", False):
            continue

        # Get labels for this card
        card_labels = []
        for label_id in card.get("idLabels", []):
            if label_id in label_map:
                label_info = label_map[label_id]
                if label_info["name"]:  # Only include labels with names
                    card_labels.append({
                        "name": label_info["name"],
                        "color": label_info["color"]
                    })

        cards.append({
            "id": card["id"],
            "name": card.get("name", "Untitled"),
            "desc": card.get("desc", ""),
            "idList": card.get("idList", ""),
            "labels": card_labels,
            "url": card.get("url", ""),
        })

    # Extract unique labels
    labels = [
        {"id": label["id"], "name": label.get("name", ""), "color": label.get("color", "")}
        for label in json_data.get("labels", [])
        if label.get("name")
    ]

    # Create list map
    list_map = {lst["id"]: lst["name"] for lst in lists}

    return {
        "board_id": board_id,
        "board_name": board_name,
        "lists": lists,
        "cards": cards,
        "labels": labels,
        "list_map": list_map,
    }


# =============================================================================
# API CLIENTS
# =============================================================================

class TrelloClient:
    """Client for interacting with the Trello REST API."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(self, api_key: str, token: str):
        self.api_key = api_key
        self.token = token

    def _make_request(self, endpoint: str, params: Optional[dict] = None) -> dict | list:
        """Make an authenticated request to the Trello API."""
        url = f"{self.BASE_URL}{endpoint}"
        params = params or {}
        params.update({"key": self.api_key, "token": self.token})

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_boards(self) -> list[dict]:
        """Fetch all boards for the authenticated user."""
        return self._make_request("/members/me/boards", {"fields": "name,id,url"})

    def get_lists(self, board_id: str) -> list[dict]:
        """Fetch all lists in a board."""
        return self._make_request(f"/boards/{board_id}/lists", {"fields": "name,id"})

    def get_cards(self, board_id: str) -> list[dict]:
        """Fetch all cards in a board with their details."""
        return self._make_request(
            f"/boards/{board_id}/cards",
            {"fields": "name,desc,idList,labels,url"}
        )

    def get_labels(self, board_id: str) -> list[dict]:
        """Fetch all labels in a board."""
        return self._make_request(f"/boards/{board_id}/labels", {"fields": "name,color,id"})


class GitHubClient:
    """Client for interacting with the GitHub REST API."""

    BASE_URL = "https://api.github.com"

    # Map Trello colors to GitHub label colors (hex without #)
    COLOR_MAP = {
        "green": "0e8a16",
        "yellow": "fbca04",
        "orange": "d93f0b",
        "red": "b60205",
        "purple": "5319e7",
        "blue": "0052cc",
        "sky": "1d76db",
        "lime": "84b817",
        "pink": "e99695",
        "black": "333333",
        None: "ededed",
        "": "ededed",
    }

    def __init__(self, token: str, owner: str, is_org: bool = False):
        self.token = token
        self.owner = owner  # username or org name
        self._is_org_override = is_org  # User's explicit choice
        self._authenticated_user = None  # Cached authenticated user login
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None
    ) -> dict | list | None:
        """Make an authenticated request to the GitHub API."""
        url = f"{self.BASE_URL}{endpoint}"

        response = requests.request(
            method,
            url,
            headers=self.headers,
            json=data,
            timeout=30
        )

        if response.status_code == 404:
            return None

        # For non-success status codes, try to extract error message
        if not response.ok:
            try:
                error_data = response.json()
                error_msg = error_data.get("message", response.text)
                errors = error_data.get("errors", [])
                if errors:
                    error_details = "; ".join(
                        e.get("message", str(e)) for e in errors
                    )
                    error_msg = f"{error_msg} ({error_details})"
            except Exception:
                error_msg = response.text

            raise requests.exceptions.HTTPError(
                f"GitHub API error {response.status_code}: {error_msg}",
                response=response
            )

        if response.status_code == 204:
            return {}

        return response.json()

    def get_authenticated_user(self) -> str:
        """Get the login name of the authenticated user."""
        if self._authenticated_user is None:
            result = self._make_request("GET", "/user")
            self._authenticated_user = result.get("login", "") if result else ""
        return self._authenticated_user

    def is_org(self) -> bool:
        """
        Determine if the owner is an organization.

        Returns True if:
        - User explicitly selected Organization mode, OR
        - The owner doesn't match the authenticated user's login
        """
        if self._is_org_override:
            return True

        # Auto-detect: if owner != authenticated user, treat as org
        auth_user = self.get_authenticated_user()
        return auth_user.lower() != self.owner.lower()

    def repo_exists(self, repo_name: str) -> bool:
        """Check if a repository exists."""
        result = self._make_request("GET", f"/repos/{self.owner}/{repo_name}")
        return result is not None

    def create_repo(self, repo_name: str, description: str = "") -> dict:
        """Create a new repository under user account or organization."""
        if self.is_org():
            endpoint = f"/orgs/{self.owner}/repos"
        else:
            endpoint = "/user/repos"

        return self._make_request("POST", endpoint, {
            "name": repo_name,
            "description": description,
            "private": False,
            "has_issues": True,
        })

    def get_milestones(self, repo_name: str) -> list[dict]:
        """Get all milestones in a repository."""
        result = self._make_request(
            "GET",
            f"/repos/{self.owner}/{repo_name}/milestones",
        )
        return result or []

    def create_milestone(self, repo_name: str, title: str, description: str = "") -> dict:
        """Create a milestone in a repository."""
        cleaned_title = clean_title(title)

        payload = {"title": cleaned_title}
        if description:
            payload["description"] = description

        return self._make_request(
            "POST",
            f"/repos/{self.owner}/{repo_name}/milestones",
            payload
        )

    def get_labels(self, repo_name: str) -> list[dict]:
        """Get all labels in a repository."""
        result = self._make_request(
            "GET",
            f"/repos/{self.owner}/{repo_name}/labels",
        )
        return result or []

    def create_label(self, repo_name: str, name: str, color: str) -> dict:
        """Create a label in a repository."""
        return self._make_request(
            "POST",
            f"/repos/{self.owner}/{repo_name}/labels",
            {"name": name, "color": color}
        )

    def create_issue(
        self,
        repo_name: str,
        title: str,
        body: str = "",
        milestone: Optional[int] = None,
        labels: Optional[list[str]] = None
    ) -> dict:
        """Create an issue in a repository."""
        cleaned_title = clean_title(title)

        data = {"title": cleaned_title, "body": body}

        if milestone:
            data["milestone"] = milestone
        if labels:
            data["labels"] = labels

        return self._make_request(
            "POST",
            f"/repos/{self.owner}/{repo_name}/issues",
            data
        )


# =============================================================================
# STREAMLIT UI
# =============================================================================

def init_session_state():
    """Initialize session state variables."""
    defaults = {
        "trello_connected": False,
        "boards": [],
        "selected_board": None,
        "board_data": None,
        "migration_complete": False,
        "migration_results": None,
        "input_mode": "api",  # "api" or "json"
        "json_loaded": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def render_sidebar() -> dict:
    """Render the sidebar with credential inputs and input mode selection."""
    with st.sidebar:
        st.header("Data Source")

        # Input mode toggle
        input_mode = st.radio(
            "How would you like to import your Trello board?",
            options=["api", "json"],
            format_func=lambda x: "Connect via Trello API" if x == "api" else "Upload Board JSON",
            key="input_mode_selector",
            horizontal=False,
        )

        # Update session state
        if st.session_state.input_mode != input_mode:
            st.session_state.input_mode = input_mode
            # Reset board data when switching modes
            st.session_state.trello_connected = False
            st.session_state.boards = []
            st.session_state.board_data = None
            st.session_state.json_loaded = False

        st.divider()

        trello_api_key = ""
        trello_token = ""
        uploaded_file = None

        if input_mode == "api":
            # Trello API credentials
            st.subheader("Trello API")
            st.caption(
                "Get your API key and token from "
                "[Trello Developer Portal](https://trello.com/power-ups/admin)"
            )

            trello_api_key = st.text_input(
                "API Key",
                type="password",
                key="trello_api_key",
                help="Your Trello API key"
            )

            trello_token = st.text_input(
                "Token",
                type="password",
                key="trello_token",
                help="Your Trello API token"
            )

        else:
            # JSON upload mode
            st.subheader("Upload JSON")

            with st.expander("How to export from Trello", expanded=False):
                st.markdown("""
                1. Open your Trello board
                2. Click **Show Menu** (top right)
                3. Click **More**
                4. Click **Print and Export**
                5. Click **Export as JSON**
                6. Save the file and upload it below
                """)

            uploaded_file = st.file_uploader(
                "Board JSON file",
                type=["json"],
                key="trello_json_upload",
                help="Upload a Trello board export (.json)"
            )

            if uploaded_file:
                st.success(f"File loaded: {uploaded_file.name}")

        st.divider()

        # GitHub credentials (always shown)
        st.subheader("GitHub")
        st.caption(
            "Generate a token at "
            "[GitHub Settings](https://github.com/settings/tokens) "
            "with `repo` scope"
        )

        github_token = st.text_input(
            "Personal Access Token",
            type="password",
            key="github_token",
            help="GitHub PAT with repo permissions"
        )

        # Account type toggle
        account_type = st.radio(
            "Account Type",
            options=["personal", "organization"],
            format_func=lambda x: "Personal Account" if x == "personal" else "Organization",
            key="github_account_type",
            horizontal=True,
        )

        is_org = account_type == "organization"

        github_owner = st.text_input(
            "Organization Name" if is_org else "Username",
            key="github_owner",
            help="GitHub organization name" if is_org else "Your GitHub username"
        )

        github_repo = st.text_input(
            "Repository Name",
            key="github_repo",
            help="Target repository name (will be created if it doesn't exist)"
        )

        st.divider()

        # Validation status
        github_ready = all([github_token, github_owner, github_repo])

        if input_mode == "api":
            trello_ready = all([trello_api_key, trello_token])
        else:
            trello_ready = uploaded_file is not None

        if github_ready and trello_ready:
            st.success("Ready to proceed")
        else:
            missing = []
            if not trello_ready:
                missing.append("Trello data source")
            if not github_ready:
                missing.append("GitHub credentials")
            st.warning(f"Missing: {', '.join(missing)}")

        return {
            "input_mode": input_mode,
            "trello_api_key": trello_api_key,
            "trello_token": trello_token,
            "uploaded_file": uploaded_file,
            "github_token": github_token,
            "github_owner": github_owner,
            "github_repo": github_repo,
            "github_is_org": is_org,
            "trello_ready": trello_ready,
            "github_ready": github_ready,
        }


def render_connect_step_api(trello_client: TrelloClient):
    """Render the Connect & Preview step for API mode."""
    st.header("Step 1: Connect & Preview")

    col1, col2 = st.columns([1, 3])

    with col1:
        if st.button("Connect to Trello", type="primary", use_container_width=True):
            with st.spinner("Fetching boards..."):
                try:
                    boards = trello_client.get_boards()
                    st.session_state.boards = boards
                    st.session_state.trello_connected = True
                    st.rerun()
                except requests.exceptions.HTTPError as e:
                    st.error(f"Failed to connect to Trello: {e}")
                except Exception as e:
                    st.error(f"Error: {e}")

    if st.session_state.trello_connected and st.session_state.boards:
        with col2:
            st.success(f"Connected! Found {len(st.session_state.boards)} boards")

        # Board selection
        board_options = {b["name"]: b["id"] for b in st.session_state.boards}
        selected_board_name = st.selectbox(
            "Select a board to migrate",
            options=list(board_options.keys()),
            key="board_selector"
        )

        if selected_board_name:
            selected_board_id = board_options[selected_board_name]

            if st.button("Load Board Data", use_container_width=False):
                with st.spinner("Fetching lists and cards..."):
                    try:
                        lists = trello_client.get_lists(selected_board_id)
                        cards = trello_client.get_cards(selected_board_id)
                        labels = trello_client.get_labels(selected_board_id)

                        # Organize cards by list
                        list_map = {lst["id"]: lst["name"] for lst in lists}

                        st.session_state.board_data = {
                            "board_id": selected_board_id,
                            "board_name": selected_board_name,
                            "lists": lists,
                            "cards": cards,
                            "labels": labels,
                            "list_map": list_map,
                        }
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error loading board: {e}")

        # Display board preview
        if st.session_state.board_data:
            render_board_preview(st.session_state.board_data)


def render_connect_step_json(uploaded_file):
    """Render the Connect & Preview step for JSON upload mode."""
    st.header("Step 1: Load & Preview")

    if not uploaded_file:
        st.info(
            "Upload a Trello board JSON export in the sidebar to preview your data."
        )

        with st.expander("How to export your Trello board as JSON", expanded=True):
            st.markdown("""
            ### Export Instructions

            1. Open your Trello board in a web browser
            2. Click the **Show Menu** button (top right corner)
            3. Click **More** to expand additional options
            4. Click **Print and Export**
            5. Click **Export as JSON**
            6. Save the downloaded `.json` file
            7. Upload it using the file uploader in the sidebar

            The JSON file contains all your lists, cards, labels, and descriptions.
            """)
        return

    # Parse the uploaded JSON
    col1, col2 = st.columns([1, 3])

    with col1:
        parse_button = st.button(
            "Parse JSON",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.json_loaded
        )

    if parse_button or st.session_state.json_loaded:
        if not st.session_state.json_loaded:
            with st.spinner("Parsing board data..."):
                try:
                    json_content = uploaded_file.read()
                    json_data = json.loads(json_content)
                    board_data = parse_trello_json(json_data)
                    st.session_state.board_data = board_data
                    st.session_state.json_loaded = True
                    st.rerun()
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON file: {e}")
                    return
                except Exception as e:
                    st.error(f"Error parsing board data: {e}")
                    return

        if st.session_state.board_data:
            with col2:
                st.success(
                    f"Loaded board: **{st.session_state.board_data['board_name']}**"
                )

            render_board_preview(st.session_state.board_data)

            # Option to reload
            if st.button("Clear and Upload Different File"):
                st.session_state.board_data = None
                st.session_state.json_loaded = False
                st.rerun()


def render_board_preview(board_data: dict):
    """Render the board preview with expandable lists."""
    st.subheader(f"Preview: {board_data['board_name']}")

    lists = board_data["lists"]
    cards = board_data["cards"]
    list_map = board_data["list_map"]

    # Group cards by list
    cards_by_list = {}
    for card in cards:
        list_id = card["idList"]
        if list_id not in cards_by_list:
            cards_by_list[list_id] = []
        cards_by_list[list_id].append(card)

    # Summary metrics
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Lists (→ Milestones)", len(lists))
    with col2:
        st.metric("Cards (→ Issues)", len(cards))
    with col3:
        unique_labels = set()
        for card in cards:
            for label in card.get("labels", []):
                if label.get("name"):
                    unique_labels.add(label["name"])
        st.metric("Labels", len(unique_labels))

    st.divider()

    # Expandable lists
    for lst in lists:
        list_cards = cards_by_list.get(lst["id"], [])
        with st.expander(f"**{lst['name']}** ({len(list_cards)} cards)", expanded=False):
            if list_cards:
                for card in list_cards:
                    st.markdown(f"**{card['name']}**")

                    # Show labels
                    labels = card.get("labels", [])
                    if labels:
                        label_names = [
                            f"`{l['name']}`"
                            for l in labels
                            if l.get("name")
                        ]
                        if label_names:
                            st.caption(f"Labels: {' '.join(label_names)}")

                    # Show description preview
                    desc = card.get("desc", "")
                    if desc:
                        preview = desc[:150] + "..." if len(desc) > 150 else desc
                        st.caption(preview)

                    st.markdown("---")
            else:
                st.caption("No cards in this list")


def render_migrate_step(
    github_client: GitHubClient,
    repo_name: str
):
    """Render the migration step."""
    st.header("Step 2: Migrate to GitHub")

    if not st.session_state.board_data:
        if st.session_state.input_mode == "api":
            st.info("Please connect to Trello and load a board first.")
        else:
            st.info("Please upload and parse a Trello board JSON file first.")
        return

    board_data = st.session_state.board_data

    st.write(
        f"Ready to migrate **{board_data['board_name']}** to "
        f"**{github_client.owner}/{repo_name}**"
    )

    # Migration summary
    with st.container(border=True):
        st.write("**Migration plan:**")
        st.write(f"- Create repository `{repo_name}` (if it doesn't exist)")
        st.write(f"- Create {len(board_data['lists'])} milestones")
        st.write(f"- Create {len(board_data['cards'])} issues")

    col1, col2 = st.columns([1, 3])

    with col1:
        migrate_button = st.button(
            "Start Migration",
            type="primary",
            use_container_width=True
        )

    if migrate_button:
        run_migration(github_client, repo_name, board_data)


def run_migration(
    github_client: GitHubClient,
    repo_name: str,
    board_data: dict
):
    """Execute the migration process."""
    results = {
        "repo_created": False,
        "milestones_created": 0,
        "labels_created": 0,
        "issues_created": 0,
        "errors": [],
        "repo_url": f"https://github.com/{github_client.owner}/{repo_name}",
    }

    lists = board_data["lists"]
    cards = board_data["cards"]
    list_map = board_data["list_map"]

    total_steps = 1 + len(lists) + len(cards)  # repo + milestones + issues
    current_step = 0

    progress_bar = st.progress(0)
    status_text = st.empty()

    try:
        # Step 1: Create or verify repository
        status_text.write("Checking repository...")

        if not github_client.repo_exists(repo_name):
            status_text.write(f"Creating repository: {repo_name}")
            github_client.create_repo(
                repo_name,
                f"Migrated from Trello board: {board_data['board_name']}"
            )
            results["repo_created"] = True
            time.sleep(1)  # Brief pause for repo creation to propagate

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # Step 2: Create milestones (BEFORE issues so we can assign issues to them)
        status_text.write("Creating milestones...")

        existing_milestones = github_client.get_milestones(repo_name)
        # Map cleaned titles to milestone numbers for comparison
        existing_milestone_names = {m["title"]: m["number"] for m in existing_milestones}

        milestone_map = {}  # list_id -> milestone_number
        error_container = st.container()  # Container for real-time error display

        for lst in lists:
            list_name = lst["name"]
            list_id = lst["id"]
            cleaned_name = clean_title(list_name)

            # Check if milestone already exists (compare cleaned names)
            if cleaned_name in existing_milestone_names:
                milestone_map[list_id] = existing_milestone_names[cleaned_name]
                status_text.write(f"Milestone exists: {cleaned_name}")
            else:
                try:
                    milestone = github_client.create_milestone(repo_name, list_name)
                    if milestone and "number" in milestone:
                        milestone_map[list_id] = milestone["number"]
                        results["milestones_created"] += 1
                        status_text.write(f"Created milestone: {cleaned_name}")
                    else:
                        error_msg = f"Milestone '{cleaned_name}' created but no number returned"
                        results["errors"].append(error_msg)
                        with error_container:
                            st.warning(error_msg)
                except Exception as e:
                    error_msg = f"Failed to create milestone '{cleaned_name}': {e}"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.error(error_msg)

            current_step += 1
            progress_bar.progress(current_step / total_steps)

        # Step 3: Create labels
        status_text.write("Creating labels...")

        existing_labels = github_client.get_labels(repo_name)
        existing_label_names = {l["name"].lower() for l in existing_labels}

        unique_labels = {}
        for card in cards:
            for label in card.get("labels", []):
                if label.get("name"):
                    unique_labels[label["name"]] = label.get("color")

        for label_name, label_color in unique_labels.items():
            if label_name.lower() not in existing_label_names:
                try:
                    color = github_client.COLOR_MAP.get(label_color, "ededed")
                    github_client.create_label(repo_name, label_name, color)
                    results["labels_created"] += 1
                except Exception as e:
                    error_msg = f"Failed to create label '{label_name}': {e}"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.error(error_msg)

        # Step 4: Create issues
        status_text.write("Creating issues...")

        for card in cards:
            card_name = card["name"]
            cleaned_card_name = clean_title(card_name)
            card_desc = card.get("desc", "")
            list_id = card["idList"]
            card_labels = [l["name"] for l in card.get("labels", []) if l.get("name")]

            # Build issue body with Trello reference
            body = card_desc
            if card.get("url"):
                body += f"\n\n---\n*Migrated from Trello: {card['url']}*"

            milestone_number = milestone_map.get(list_id)

            try:
                github_client.create_issue(
                    repo_name,
                    card_name,  # clean_title is called inside create_issue
                    body,
                    milestone=milestone_number,
                    labels=card_labels if card_labels else None
                )
                results["issues_created"] += 1
                status_text.write(f"Created issue: {cleaned_card_name}")
            except Exception as e:
                error_msg = f"Failed to create issue '{cleaned_card_name}': {e}"
                results["errors"].append(error_msg)
                with error_container:
                    st.error(error_msg)

            current_step += 1
            progress_bar.progress(current_step / total_steps)

            # Small delay to avoid rate limiting
            time.sleep(0.5)

        progress_bar.progress(1.0)
        status_text.write("Migration complete!")

    except Exception as e:
        results["errors"].append(f"Migration failed: {e}")
        status_text.error(f"Migration error: {e}")

    st.session_state.migration_complete = True
    st.session_state.migration_results = results
    st.rerun()


def render_results():
    """Render the migration results."""
    st.header("Migration Results")

    results = st.session_state.migration_results

    if not results:
        return

    # Success metrics
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Repository", "Created" if results["repo_created"] else "Existed")
    with col2:
        st.metric("Milestones", results["milestones_created"])
    with col3:
        st.metric("Labels", results["labels_created"])
    with col4:
        st.metric("Issues", results["issues_created"])

    # Errors
    if results["errors"]:
        st.subheader("Errors")
        for error in results["errors"]:
            st.error(error)
    else:
        st.success("Migration completed successfully!")

    # Repository link
    st.divider()
    st.subheader("Your Repository")
    st.markdown(f"[**Open {results['repo_url']}**]({results['repo_url']})")

    # Reset button
    if st.button("Start New Migration"):
        st.session_state.trello_connected = False
        st.session_state.boards = []
        st.session_state.selected_board = None
        st.session_state.board_data = None
        st.session_state.migration_complete = False
        st.session_state.migration_results = None
        st.session_state.json_loaded = False
        st.rerun()


def main():
    """Main application entry point."""
    st.set_page_config(
        page_title="Trello → GitHub Migrator",
        page_icon="🔄",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()

    # Header
    st.title("Trello → GitHub Migrator")
    st.caption(
        "Migrate your Trello boards to GitHub Issues and Milestones with ease."
    )

    st.divider()

    # Sidebar - returns dict with all settings
    sidebar_config = render_sidebar()

    input_mode = sidebar_config["input_mode"]
    trello_ready = sidebar_config["trello_ready"]
    github_ready = sidebar_config["github_ready"]

    # Check if ready to proceed
    if not github_ready:
        st.info(
            "Please fill in your GitHub credentials in the sidebar to get started."
        )

        with st.expander("How to get GitHub credentials", expanded=True):
            st.markdown("""
            ### GitHub Personal Access Token

            1. Go to [GitHub Settings → Developer Settings → Personal Access Tokens](https://github.com/settings/tokens)
            2. Click "Generate new token (classic)"
            3. Give it a descriptive name
            4. Select the `repo` scope (full control of private repositories)
            5. Click "Generate token" and copy it immediately

            ### GitHub Username & Repository

            - **Username**: Your GitHub username (e.g., `octocat`)
            - **Repository Name**: The name for the new repo (will be created if it doesn't exist)
            """)
        return

    if not trello_ready and input_mode == "api":
        st.info(
            "Please fill in your Trello API credentials in the sidebar."
        )

        with st.expander("How to get Trello API credentials", expanded=True):
            st.markdown("""
            ### Trello API Credentials

            1. Go to [Trello Power-Ups Admin](https://trello.com/power-ups/admin)
            2. Click "New" to create a new Power-Up (or use an existing one)
            3. Copy your **API Key** from the API Key section
            4. Click "Generate a Token" and authorize the app
            5. Copy the **Token** that appears
            """)
        return

    # Initialize GitHub client
    github_client = GitHubClient(
        sidebar_config["github_token"],
        sidebar_config["github_owner"],
        sidebar_config["github_is_org"]
    )
    github_repo = sidebar_config["github_repo"]

    # Show results if migration is complete
    if st.session_state.migration_complete:
        render_results()
        return

    # Main workflow based on input mode
    if input_mode == "api":
        trello_client = TrelloClient(
            sidebar_config["trello_api_key"],
            sidebar_config["trello_token"]
        )
        render_connect_step_api(trello_client)
    else:
        render_connect_step_json(sidebar_config["uploaded_file"])

    st.divider()

    render_migrate_step(github_client, github_repo)


if __name__ == "__main__":
    main()
